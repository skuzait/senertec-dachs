"""High-level client for the Dachs Gen2 GLT (MSR3) Modbus interface.

The Dachs Gen2 (G2.9/G5.5/F3.0/F5.5, MSR3 controller) exposes its GLT
interface as Modbus TCP on port 502 (spec ``8098.119.000.02``, §4.3).
This client reads the documented register area with one FC 0x04
request, decodes every register onto the shared unit definitions, and
projects the values that have a proven same-key identity in the
MSR2/118-key catalogue onto the common facade (:meth:`read_snapshot`,
like local/cloud backends — the remaining GLT-only values live on this
client).

Write accesses follow §5/§6.3: the GLT pin must accompany the write
section (canonical: it is written first, and re-sent after every
connection drop), the power setpoint is written in 10 W steps and must
be refreshed cyclically within ten minutes or the unit discards all
external values (heartbeat). The echo of a write frame proves
delivery, NOT that the controller logically accepted the value —
acceptance criteria (pin known, spacing, plant protection) live at the
controller end and are outside the protocol.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from ..errors import WriteError
from ..models import Snapshot
from .modbus import ModbusTcpClient
from .regs import (
    READ_BASE,
    READ_END,
    GLT_BY_NAME,
    RegSpec,
    GltKind,
)

__all__ = (
    "DEFAULT_GL_TIMEOUT",
    "GltClient",
    "GltProfile",
    "GltStatus",
    "GltModuleCounts",
    "GltSecondGenerator",
)

DEFAULT_GL_TIMEOUT = 4.0

#: Heartbeat default: the setpoint must be renewed within ten minutes
#: (§6.3.2); the manual recommends roughly 50% of the timeout as the
#: send interval.
DEFAULT_HEARTBEAT_SECONDS = 300.0

_DEVICE_TYPE_LSB = {1: "Dachs 5.5 (G5.5)", 2: "Dachs 2.9 (G2.9)"}


def _normal_text(value: Decimal) -> str:
    text = str(value)
    if "." in text:
        return text.rstrip("0").rstrip(".") or "0"
    return text


@dataclass(frozen=True, slots=True)
class GltProfile:
    """Plant identity profile from the read area (§6.2.1)."""

    glt_version: str | None
    device_type_raw: int | None
    device_type_name: str | None
    serial_number: str | None
    rated_power_watts: int | None


@dataclass(frozen=True, slots=True)
class GltStatus:
    """Operating status of the plant (§6.2.2)."""

    plant_status_raw: int | None
    plant_status_text: str | None
    request_kind_raw: int | None
    request_kind_text: str | None
    shutdown_reason_raw: int | None
    shutdown_reason_text: str | None
    electric_output_watts: Decimal | None
    heating_pump_raw: int | None
    runtime_since_start_hours: Decimal | None


@dataclass(frozen=True, slots=True)
class GltModuleCounts:
    """Multi-module plant inventory (§6.2.7) or a single-module plant."""

    detected: int | None
    available: int | None
    requested: int | None
    running: int | None
    module_count: int | None
    total_power_kw: Decimal | None
    rated_power_kw: Decimal | None


@dataclass(frozen=True, slots=True)
class GltSecondGenerator:
    """Second heat generator block (§6.2.8), all raw + text pairs."""

    status_raw: int | None
    status_text: str | None
    target_raw: int | None
    target_text: str | None
    starts: int | None
    hours: Decimal | None
    available_text: str | None
    released_text: str | None
    rated_power_kw: Decimal | None
    min_runtime_minutes: int | None
    on_buffer_text: str | None


def _enum_text(spec: RegSpec, raw: int | None) -> str | None:
    if raw is None or spec.enum is None:
        return None
    return spec.enum.get(raw)


class GltClient:
    """The Dachs Gen2 GLT client: facade-compatible raw + typed access.

    Typical loop::

        async with GltClient("192.168.1.42", pin=1234) as client:
            snapshot = await client.update()      # one block read
            print(client.snapshot.operating.runtime.value)
            await client.set_power_setpoint(5500) # documents §6.3.2
            heartbeat = client.start_heartbeat()  # keeps it alive
            ...
            heartbeat.cancel()
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        unit_id: int = 1,
        timeout: float = DEFAULT_GL_TIMEOUT,
        pin: int | None = None,
    ) -> None:
        self._modbus = ModbusTcpClient(
            host, port, unit_id=unit_id, timeout=timeout
        )
        if pin is not None and not 0 <= pin <= 9999:
            raise ValueError("The GLT pin is the 4-digit NP222 value")
        self._pin = pin
        self._pin_sent = False
        self._written_setpoint: int | None = None
        self._written_lock: bool | None = None
        self._decoded: dict[str, int | str | None] = {}
        self._snapshot: Snapshot | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the Modbus TCP socket is currently open."""
        return self._modbus.connected

    async def close(self) -> None:
        """Close the TCP connection; values keep cached results."""
        await self._modbus.close()

    async def __aenter__(self) -> GltClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # -- reads -----------------------------------------------------------------

    async def read_snapshot(self) -> Snapshot:
        """Facade-compatible read: whole register area, decoded snapshot.

        One FC 0x04 request for registers 8000..8083 (§6.2). The
        manual recommends keeping request pacing at 1000 ms or slower.
        """
        return await self.update()

    async def update(self) -> Snapshot:
        """Read the whole documented register area and refresh state.

        One FC 0x04 request for registers 8000..8083 (§6.2). Keep the
        manual's recommended pacing of one request per second or
        slower. Returns the facade-compatible snapshot; ``snapshot``
        caches until the next successful read.
        """
        registers = await self._modbus.read_input_registers(
            READ_BASE, READ_END - READ_BASE + 1
        )
        self._decode_registers(registers)
        self._snapshot = Snapshot.from_raw(self._msr2_raw())
        return self._snapshot

    def _word(self, spec: RegSpec) -> int | None:
        raw = self._decoded.get(spec.name)
        return None if isinstance(raw, str) or raw is None else raw

    def _value_of(self, spec: RegSpec) -> int | Decimal | str | None:
        raw = self._decoded.get(spec.name)
        if raw is None or isinstance(raw, str):
            return None if raw is None else raw
        if spec.factor is not None:
            return None if raw is None else Decimal(raw) * spec.factor
        return raw

    def _decode_registers(self, registers: Sequence[int]) -> None:
        base = READ_BASE

        def word(address: int) -> int:
            return registers[address - base]

        for spec in GLT_BY_NAME.values():
            if spec.kind is GltKind.STR20:
                data = bytearray()
                for addr in range(spec.address, spec.address + 10):
                    value = word(addr)
                    data.append((value >> 8) & 0xFF)
                    data.append(value & 0xFF)
                text = bytes(data).split(b"\x00", 1)[0].decode("utf-8", "replace")
                self._decoded[spec.name] = text
                continue
            if spec.kind is GltKind.U32:
                high, low = word(spec.address), word(spec.address + 1)
                self._decoded[spec.name] = (high << 16) | low
                continue
            value = word(spec.address)
            if spec.kind is GltKind.U8:
                value = value & 0xFF
            elif spec.kind is GltKind.I16:
                value = value - 0x10000 if value >= 0x8000 else value
            self._decoded[spec.name] = value

    def _msr2_raw(self) -> dict[str, str]:
        """Project GLT registers onto shared MSR2 wire-key texts.

        Only keys whose GLT parameter identity matches the MSR2 key's
        documented quantity are projected (each catalogue entry cites
        the identity); everything the GLT register map does not carry
        simply stays absent, which surfaces as honest MISSING values.
        """
        raw: dict[str, str] = {}
        for name, spec in GLT_BY_NAME.items():
            if spec.msr2_key is None:
                continue
            raw_value = self._decoded.get(name)
            if raw_value is None:
                continue
            if isinstance(raw_value, str):
                raw[spec.msr2_key] = raw_value
            elif spec.factor is None:
                raw[spec.msr2_key] = _normal_text(Decimal(raw_value))
            else:
                raw[spec.msr2_key] = _normal_text(
                    Decimal(raw_value) * spec.factor
                )
        return raw

    @property
    def snapshot(self) -> Snapshot:
        """The last successful :meth:`update` snapshot."""
        if self._snapshot is None:
            raise RuntimeError("No data yet - await client.update() first")
        return self._snapshot

    # -- typed GLT-native values ---------------------------------------------

    def profile(self) -> GltProfile:
        version_raw = self._word(GLT_BY_NAME["glt_version"])
        device_raw = self._word(GLT_BY_NAME["device_type"])
        serial = self._decoded.get("serial_number")
        return GltProfile(
            glt_version=(
                f"{version_raw >> 8}.{version_raw & 0xFF}"
                if version_raw is not None
                else None
            ),
            device_type_raw=device_raw,
            device_type_name=(
                _DEVICE_TYPE_LSB.get(device_raw & 0xFF, None)
                if device_raw is not None
                else None
            ),
            serial_number=serial if isinstance(serial, str) else None,
            rated_power_watts=self._word(GLT_BY_NAME["rated_power_watts"]),
        )

    def status(self) -> GltStatus:
        watts = self._value_of(GLT_BY_NAME["electric_output_kw"])
        hours = self._value_of(GLT_BY_NAME["runtime_since_start_hours"])
        return GltStatus(
            plant_status_raw=self._word(GLT_BY_NAME["plant_status_raw"]),
            plant_status_text=_enum_text(
                GLT_BY_NAME["plant_status_raw"],
                self._word(GLT_BY_NAME["plant_status_raw"]),
            ),
            request_kind_raw=self._word(GLT_BY_NAME["request_kind_raw"]),
            request_kind_text=_enum_text(
                GLT_BY_NAME["request_kind_raw"],
                self._word(GLT_BY_NAME["request_kind_raw"]),
            ),
            shutdown_reason_raw=self._word(GLT_BY_NAME["shutdown_reason_raw"]),
            shutdown_reason_text=_enum_text(
                GLT_BY_NAME["shutdown_reason_raw"],
                self._word(GLT_BY_NAME["shutdown_reason_raw"]),
            ),
            electric_output_watts=(
                watts * 100 if isinstance(watts, Decimal) else None
            ),
            heating_pump_raw=self._word(GLT_BY_NAME["heating_water_pump_raw"]),
            runtime_since_start_hours=(
                hours if isinstance(hours, Decimal) else None
            ),
        )

    def module_counts(self) -> GltModuleCounts:

        def count(name: str) -> int | None:
            return self._word(GLT_BY_NAME[name])

        def kw(name: str) -> Decimal | None:
            value = self._value_of(GLT_BY_NAME[name])
            return value if isinstance(value, Decimal) else None

        return GltModuleCounts(
            detected=count("multi_modules_detected"),
            available=count("multi_modules_available"),
            requested=count("multi_modules_requested"),
            running=count("multi_modules_running"),
            module_count=count("multi_module_count"),
            total_power_kw=kw("multi_power_kw"),
            rated_power_kw=kw("multi_rated_power_kw"),
        )

    def second_generator(self) -> GltSecondGenerator:
        by_name = GLT_BY_NAME
        hours = self._value_of(by_name["second_generator_hours"])
        rated = self._value_of(by_name["second_generator_rated_kw"])
        return GltSecondGenerator(
            status_raw=self._word(by_name["second_generator_status_raw"]),
            status_text=_enum_text(
                by_name["second_generator_status_raw"],
                self._word(by_name["second_generator_status_raw"]),
            ),
            target_raw=self._word(by_name["second_generator_target_raw"]),
            target_text=_enum_text(
                by_name["second_generator_target_raw"],
                self._word(by_name["second_generator_target_raw"]),
            ),
            starts=self._word(by_name["second_generator_starts"]),
            hours=hours if isinstance(hours, Decimal) else None,
            available_text=_enum_text(
                by_name["second_generator_available_raw"],
                self._word(by_name["second_generator_available_raw"]),
            ),
            released_text=_enum_text(
                by_name["second_generator_released_raw"],
                self._word(by_name["second_generator_released_raw"]),
            ),
            rated_power_kw=rated if isinstance(rated, Decimal) else None,
            min_runtime_minutes=self._word(
                by_name["second_generator_min_runtime_min"]
            ),
            on_buffer_text=_enum_text(
                by_name["second_generator_on_buffer_raw"],
                self._word(by_name["second_generator_on_buffer_raw"]),
            ),
        )

    # -- writes (§5, §6.3) ------------------------------------------------------

    async def _ensure_pin_sent(self) -> None:
        """GLT pin must precede external writes (§6.3.1).

        Writes with a configured pin re-send the pin after every
        (re)connection since the unit forgets it on connection loss.
        """
        if self._pin is None:
            return
        if not self._pin_sent or not self._modbus.connected:
            await self._modbus.write_register(8300, self._pin)
            self._pin_sent = True

    async def set_pin(self, pin: int) -> None:
        """Transmit the 4-digit GLT pin (register 8300, §6.3.1).

        The pin must be re-sent whenever the TCP connection is
        re-established; passing ``pin`` to the constructor makes every
        write path resend it automatically.
        """
        if not 0 <= pin <= 9999:
            raise ValueError("The GLT pin is the 4-digit NP222 value")
        await self._modbus.write_register(8300, pin)
        self._pin = pin
        self._pin_sent = bool(self._modbus.connected)

    async def set_power_setpoint(self, watts: int) -> None:
        """Write the electric power demand in watts (register 8301).

        Per §6.3.2 the value is given in 10 W steps; changes are only
        accepted five minutes apart, and the value must be renewed
        within ten minutes (:meth:`start_heartbeat`) or every external
        control value is discarded. The frame echo proves delivery —
        logical acceptance depends on the unit (pin, plant protection,
        spacing) and cannot be read back via this protocol. The next
        :meth:`update` shows whether the unit moves toward the
        demand.
        """
        if not isinstance(watts, int):
            raise ValueError("The power setpoint writes whole watts")
        if watts < 0 or watts % 10:
            raise ValueError("The setpoint is written in 10 W steps")
        await self._ensure_pin_sent()
        await self._modbus.write_register(8301, watts)
        self._written_setpoint = watts

    async def set_locked(self, locked: bool) -> None:
        """Write the GLT start lock (register 8302, §6.3.3).

        ``True`` blocks the CHP from starting. The lock decays together
        with the power-setpoint heartbeat.
        """
        await self._ensure_pin_sent()
        await self._modbus.write_register(8302, 1 if locked else 0)
        self._written_lock = locked

    def start_heartbeat(
        self, interval: float = DEFAULT_HEARTBEAT_SECONDS
    ) -> asyncio.Task[None]:
        """Keep external control values alive (§6.3.2 heartbeat).

        Rewrites the last written setpoint and lock state every
        ``interval`` seconds (default 300 s, manual recommendation:
        roughly 50% of the ten-minute timeout). The task runs until
        cancelled. If nothing has been written yet, ticks are no-ops —
        the client never invents control values.
        """

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                await self._heartbeat_tick()

        return asyncio.ensure_future(_loop())

    async def _heartbeat_tick(self) -> None:
        if self._written_setpoint is None and self._written_lock is None:
            return
        await self._ensure_pin_sent()
        if self._written_setpoint is not None:
            await self._modbus.write_register(8301, self._written_setpoint)
        if self._written_lock is not None:
            await self._modbus.write_register(
                8302, 1 if self._written_lock else 0
            )

    # -- facade parity hooks (honest failures like cloud v2) -------------------

    async def request_run(self) -> None:
        raise WriteError(
            "The Gen2 GLT interface has no one-shot run request; demand "
            "power with set_power_setpoint() (documented §6.3.2) and "
            "keep it alive with a heartbeat",
            outcome_unknown=False,
        )

    async def request_power(self, modules: int) -> None:
        raise WriteError(
            "The Gen2 GLT interface demands WATTS, not modules - use "
            "set_power_setpoint(watts) (documented §6.3.2)",
            outcome_unknown=False,
        )

    # -- misc --------------------------------------------------------------------

    @property
    def last_values(self) -> Mapping[str, int | str | None]:
        """Raw decoded register values of the last :meth:`update`."""
        return dict(self._decoded)
