"""The Dachs device object: one unit, direct typed access.

``Dachs`` wraps a :class:`~senertec_dachs.local.client.LocalClient` and the
last :class:`~senertec_dachs.models.Snapshot`. The common path is::

    async with Dachs("192.168.1.50") as dachs:
        await dachs.update()
        print(dachs.serial_number)      # plain values
        print(dachs.power_kw)

Coverage: every documented key whose scale is confirmed has a property.
Every property returns ``None`` unless the underlying reading is
``AVAILABLE``. The ten keys whose scale is still unverified (hydraulic
configuration codes, ``bWwPumpe``, yearly hot-water volume, and the
``Mm_MinMax`` runtime quantities) deliberately have no property — they
could only ever return ``None``; use ``dachs.snapshot`` (states, raw text,
all 118 keys) or ``dachs.client`` (raw reads, ``request_power``) for those
and for everything else. A future cloud backend can sit behind the same
device object.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import TypeVar

import aiohttp

from .cloud.client import (
    DEFAULT_MODULE as DEFAULT_CLOUD_MODULE,
    DEFAULT_TIMEOUT as DEFAULT_CLOUD_TIMEOUT,
    CloudClient,
    CloudStatus,
)
from .cloud.protocol import BASE_URL
from .errors import DachsError
from .local.client import (
    DEFAULT_MAX_URL_LENGTH,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_USERNAME,
    LocalClient,
)
from .models import Reading, ReadingState, Snapshot

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ModuleStatus:
    """Friendly per-module status (plain values, ``None`` when unknown)."""

    running: bool | None
    available: bool | None


class Dachs:
    """One Dachs unit: :meth:`update` once, then direct typed access.

    Local access: ``Dachs("192.168.1.50")`` — the MSR2 GLT interface on
    your LAN, live values. Cloud access: ``Dachs.cloud("12345", "…")`` —
    the Dachsportal account; values are the portal's cached polls of the
    device, so check :attr:`device_online` and :attr:`data_fetched_at`
    for freshness. Both share the exact same API.

    Pass ``host`` (plus optional connection keyword arguments) to create
    an owned local client, or pass an existing ``client`` — which then
    stays owned by the caller and is not closed by this object.
    """

    def __init__(
        self,
        host: str | None = None,
        *,
        client: LocalClient | CloudClient | None = None,
        port: int = DEFAULT_PORT,
        username: str = DEFAULT_USERNAME,
        password: str = "",
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_url_length: int = DEFAULT_MAX_URL_LENGTH,
    ) -> None:
        if client is not None:
            if host is not None:
                raise ValueError("Pass either host or client, not both")
            self._client = client
            self._owns_client = False
        else:
            if host is None:
                raise ValueError("host is required when no client is given")
            self._client = LocalClient(
                host,
                port=port,
                username=username,
                password=password,
                session=session,
                timeout=timeout,
                max_url_length=max_url_length,
            )
            self._owns_client = True
        self._snapshot: Snapshot | None = None
        self._device_online: bool | None = None
        self._data_fetched_at: datetime | None = None

    @property
    def client(self) -> LocalClient | CloudClient:
        """The underlying client (raw reads, ``request_power``, extras)."""
        return self._client

    @classmethod
    def cloud(
        cls,
        portal_id: str,
        password: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_CLOUD_TIMEOUT,
        module: str = DEFAULT_CLOUD_MODULE,
        base_url: str = BASE_URL,
    ) -> Dachs:
        """Create a Dachs device object backed by the Dachsportal (cloud).

        ``portal_id`` is the trailing part of the Dachs serial number;
        no local network access is needed.
        """
        device = cls(
            client=CloudClient(
                portal_id,
                password,
                base_url=base_url,
                module=module,
                session=session,
                timeout=timeout,
            )
        )
        # The client was created here, not by the caller, so this device
        # object owns (and closes) it.
        device._owns_client = True
        return device

    @property
    def snapshot(self) -> Snapshot:
        """The last :meth:`update` result (all 118 keys with states)."""
        if self._snapshot is None:
            raise RuntimeError("No data yet — await dachs.update() first")
        return self._snapshot

    async def update(self) -> Snapshot:
        """Read all documented keys and refresh the cached snapshot.

        The snapshot is stored only on success; a failed update leaves the
        previous snapshot in place and raises.

        For the cloud backend this also refreshes :attr:`device_online`
        and :attr:`data_fetched_at` from the portal's own status. For the
        local backend the timestamp is the read time and the device counts
        as online — a value that is only set after a successful response;
        a failed update (device unreachable, timeout, rejected
        credentials) sets :attr:`device_online` to ``False`` before the
        error propagates.
        """
        try:
            snapshot = await self._client.read_snapshot()
        except DachsError:
            self._device_online = False
            raise
        self._snapshot = snapshot
        self._data_fetched_at = datetime.now(timezone.utc)
        self._device_online = True
        if isinstance(self._client, CloudClient):
            status = self._client.last_status
            if status is not None:
                if status.device_online is not None:
                    self._device_online = status.device_online
                if status.last_fetched is not None:
                    self._data_fetched_at = status.last_fetched
        return self._snapshot

    @property
    def device_online(self) -> bool | None:
        """Whether the device was reachable for the data source.

        Local reads are live, so this is ``True`` after a successful
        :meth:`update` and ``False`` after a failed one (the error still
        propagates). The portal polls the device; when it could not reach
        it, this is ``False`` and the snapshot data is stale. ``None``
        before the first update or when the portal did not say.
        """
        return self._device_online

    @property
    def data_fetched_at(self) -> datetime | None:
        """When the snapshot data was fetched (timezone-aware UTC).

        Local: the time of :meth:`update`. Cloud: the portal's
        ``fetchedAt`` — the device poll the values come from, which can
        be minutes in the past.
        """
        return self._data_fetched_at

    async def close(self) -> None:
        """Close the device object; only clients created by it are closed."""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Dachs:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def request_run(self) -> None:
        """Issue a one-shot run request — the same call for local and cloud.

        Local backend: the documented GLT demand with one module. Cloud
        backend: the app's ``dachsAnfordern`` (the portal applies its own
        default demand). Both raise with ``outcome_unknown=True`` when
        the outcome cannot be confirmed and never retry automatically.
        """
        await self._client.request_run()

    async def request_power(self, modules: int) -> None:
        """Request external power demand for ``modules`` modules (0-10).

        Works on both backends. On the cloud backend this GLT-key path
        is unconfirmed against a real portal — treat its first real use
        as an experiment. The demand is valid
        for ten minutes; renew at a shorter interval for sustained
        demand. A clean acknowledgement proves acceptance, not that a
        module actually started.
        """
        await self._client.request_power(modules)

    def _available(self, reading: Reading[T]) -> T | None:
        return reading.value if reading.state is ReadingState.AVAILABLE else None

    # --- identity ----------------------------------------------------------

    @property
    def serial_number(self) -> str | None:
        return self._available(self.snapshot.identity.serial_number)

    @property
    def part_number(self) -> str | None:
        return self._available(self.snapshot.identity.part_number)

    @property
    def commissioned_epoch_seconds(self) -> int | None:
        """Commissioning date as seconds since 2000-01-01 (no timezone)."""
        return self._available(self.snapshot.identity.commissioned_epoch_seconds)

    # --- operating counters -------------------------------------------------

    @property
    def runtime_hours(self) -> Decimal | None:
        return self._available(self.snapshot.operating.runtime)

    @property
    def starts(self) -> int | None:
        return self._available(self.snapshot.operating.starts)

    @property
    def electrical_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.operating.electrical_energy)

    @property
    def thermal_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.operating.thermal_energy)

    @property
    def condenser_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.operating.condenser_energy)

    @property
    def fault_code(self) -> int | None:
        return self._available(self.snapshot.operating.fault_raw)

    @property
    def warning_code(self) -> int | None:
        return self._available(self.snapshot.operating.warning_raw)

    @property
    def power_requested(self) -> bool | None:
        return self._available(self.snapshot.operating.power_requested)

    @property
    def requested_modules(self) -> int | None:
        return self._available(self.snapshot.operating.requested_modules)

    @property
    def power_demand_flags(self) -> int | None:
        """Bit-coded power-demand flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.operating.power_request_mask)

    @property
    def power_release_flags(self) -> int | None:
        """Bit-coded power-release flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.operating.power_release_mask)

    @property
    def demand_flags(self) -> int | None:
        """Bit-coded Dachs demand flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.operating.request_mask)

    @property
    def release_flags(self) -> int | None:
        """Bit-coded Dachs release flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.operating.release_mask)

    # --- year-end counters (31.12.) ------------------------------------------

    @property
    def year_end_runtime_hours(self) -> Decimal | None:
        return self._available(self.snapshot.year_end.runtime)

    @property
    def year_end_starts(self) -> int | None:
        return self._available(self.snapshot.year_end.starts)

    @property
    def year_end_electrical_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.year_end.electrical_energy)

    @property
    def year_end_thermal_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.year_end.thermal_energy)

    @property
    def year_end_condenser_energy_kwh(self) -> Decimal | None:
        return self._available(self.snapshot.year_end.condenser_energy)

    # --- second heat generator (SEplus) --------------------------------------

    @property
    def second_generator_status_code(self) -> int | None:
        return self._available(self.snapshot.second_generator.status_raw)

    @property
    def second_generator_warning_code(self) -> int | None:
        return self._available(self.snapshot.second_generator.warning_raw)

    @property
    def second_generator_starts(self) -> int | None:
        return self._available(self.snapshot.second_generator.starts)

    @property
    def second_generator_runtime_hours(self) -> Decimal | None:
        return self._available(self.snapshot.second_generator.runtime)

    @property
    def second_generator_demand_flags(self) -> int | None:
        """Bit-coded demand flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.second_generator.request_mask)

    @property
    def second_generator_release_flags(self) -> int | None:
        """Bit-coded release flags; meanings per the MSR2 manual."""
        return self._available(self.snapshot.second_generator.release_mask)

    # --- measurements --------------------------------------------------------

    @property
    def power_kw(self) -> Decimal | None:
        return self._available(self.snapshot.measurements.active_power)

    @property
    def engine_speed_rpm(self) -> int | None:
        return self._available(self.snapshot.measurements.engine_speed)

    @property
    def engine_runtime_hours(self) -> Decimal | None:
        """Engine runtime since the last start, in hours (1/1000 precision).

        The MSR2 PDF says seconds and the key name agrees, but the device
        reports hours — same convention as the operating-hours counter.
        The value freezes while the engine is stopped.
        """
        return self._available(self.snapshot.measurements.runtime_since_start)

    # --- temperatures --------------------------------------------------------

    @property
    def outdoor_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.outdoor)

    @property
    def supply_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.supply_flow)

    @property
    def return_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.return_flow)

    @property
    def hot_water_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.hot_water)

    @property
    def exhaust_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.exhaust)

    @property
    def engine_exhaust_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.engine_exhaust)

    @property
    def enclosure_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.enclosure)

    @property
    def module_release_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.module_release)

    @property
    def generator_inlet_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.generator_inlet)

    @property
    def engine_coolant_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.engine_coolant)

    @property
    def controller_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.controller)

    @property
    def sensor_1_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.sensor_1)

    @property
    def sensor_2_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.sensor_2)

    @property
    def sensor_3_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.sensor_3)

    @property
    def sensor_4_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.sensor_4)

    @property
    def heating_circuit_1_supply_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.heating_circuit_1_supply)

    @property
    def heating_circuit_2_supply_temperature(self) -> Decimal | None:
        return self._available(self.snapshot.temperatures.heating_circuit_2_supply)

    # --- temperature setpoints ------------------------------------------------

    @property
    def supply_temperature_setpoint(self) -> Decimal | None:
        return self._available(self.snapshot.temperature_targets.supply_flow)

    @property
    def return_temperature_setpoint(self) -> Decimal | None:
        return self._available(self.snapshot.temperature_targets.return_flow)

    # --- actuators --------------------------------------------------------------

    @property
    def alternate_heat_enabled(self) -> bool | None:
        return self._available(self.snapshot.actuators.alternate_heat_enabled)

    @property
    def mixer_1_opening(self) -> bool | None:
        return self._available(self.snapshot.actuators.mixer_1_open)

    @property
    def mixer_1_closing(self) -> bool | None:
        return self._available(self.snapshot.actuators.mixer_1_close)

    @property
    def mixer_2_opening(self) -> bool | None:
        return self._available(self.snapshot.actuators.mixer_2_open)

    @property
    def mixer_2_closing(self) -> bool | None:
        return self._available(self.snapshot.actuators.mixer_2_close)

    @property
    def programmable_output_1(self) -> bool | None:
        return self._available(self.snapshot.actuators.programmable_output_1)

    @property
    def programmable_output_2(self) -> bool | None:
        return self._available(self.snapshot.actuators.programmable_output_2)

    @property
    def programmable_output_3(self) -> bool | None:
        return self._available(self.snapshot.actuators.programmable_output_3)

    @property
    def fault_relay(self) -> bool | None:
        return self._available(self.snapshot.actuators.fault_relay)

    @property
    def heating_circuit_1_pump(self) -> bool | None:
        return self._available(self.snapshot.actuators.heating_circuit_1_pump)

    @property
    def heating_circuit_2_pump(self) -> bool | None:
        return self._available(self.snapshot.actuators.heating_circuit_2_pump)

    @property
    def internal_circulation_pump(self) -> bool | None:
        return self._available(self.snapshot.actuators.cooling_pump)

    @property
    def pre_pressure_pump(self) -> bool | None:
        return self._available(self.snapshot.actuators.prepressure_pump)

    @property
    def circulation_pump(self) -> bool | None:
        return self._available(self.snapshot.actuators.circulation_pump)

    @property
    def maintenance_relay(self) -> bool | None:
        return self._available(self.snapshot.actuators.maintenance_relay)

    # --- modules ------------------------------------------------------------------

    @property
    def running(self) -> bool | None:
        """Whether module 1 (the unit itself) is running."""
        return self._available(self.snapshot.modules[0].running)

    @property
    def modules(self) -> tuple[ModuleStatus, ...]:
        """All 10 module slots in wire order (missing slots stay ``None``)."""
        return tuple(
            ModuleStatus(
                running=self._available(module.running),
                available=self._available(module.available),
            )
            for module in self.snapshot.modules
        )

    @property
    def daily_run_grid(self) -> tuple[int | None, ...]:
        """Twelve bit masks, one per double-hour slot, in wire order.

        Each bit covers one 15-minute interval of the slot; bit orientation
        per the MSR2 manual. Slot start hours: 02,04,…,22,00.
        """
        return tuple(self._available(slot) for slot in self.snapshot.daily_run)

    # --- module statistics -----------------------------------------------------------

    @property
    def max_maintenance_module(self) -> int | None:
        """Module number with the most runtime until its maintenance."""
        return self._available(self.snapshot.module_statistics.max_maintenance_module)

    @property
    def min_maintenance_module(self) -> int | None:
        """Module number with the least runtime until its maintenance."""
        return self._available(self.snapshot.module_statistics.min_maintenance_module)

    @property
    def max_runtime_module(self) -> int | None:
        return self._available(self.snapshot.module_statistics.max_runtime_module)

    @property
    def min_runtime_module(self) -> int | None:
        return self._available(self.snapshot.module_statistics.min_runtime_module)

    @property
    def most_starts_module(self) -> int | None:
        return self._available(self.snapshot.module_statistics.max_starts_module)

    @property
    def most_starts(self) -> int | None:
        return self._available(self.snapshot.module_statistics.max_starts)

    @property
    def fewest_starts_module(self) -> int | None:
        return self._available(self.snapshot.module_statistics.min_starts_module)

    @property
    def fewest_starts(self) -> int | None:
        return self._available(self.snapshot.module_statistics.min_starts)

    # --- maintenance --------------------------------------------------------------------

    @property
    def maintenance_due(self) -> bool | None:
        return self._available(self.snapshot.maintenance.due)

    @property
    def last_maintenance_runtime_hours(self) -> Decimal | None:
        return self._available(self.snapshot.maintenance.last_runtime)

    @property
    def last_maintenance_epoch_seconds(self) -> int | None:
        """Last maintenance as seconds since 2000-01-01 (no timezone)."""
        return self._available(self.snapshot.maintenance.last_epoch_seconds)

    @property
    def maintenance_interval_hours(self) -> int | None:
        return self._available(self.snapshot.maintenance.interval)
