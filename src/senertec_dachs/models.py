"""Immutable typed model for SenerTec Dachs MSR2 local readings.

The MSR2 GLT interface answers with plain ``key=value`` text lines. This
module maps those wire values onto typed :class:`Reading` objects. Values
whose scale or unit is not evidenced by the interface documentation are
never silently normalized: they surface as
:class:`ReadingState.UNVERIFIED` with the raw text preserved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Generic, TypeVar

T = TypeVar("T")

_INTEGER_TOKEN = re.compile(r"^[+-]?\d+$")
_DECIMAL_TOKEN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_EXPONENT_TOKEN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$")

#: Slot start hours of ``Laufraster15Min_aktTag.bDoppelstunde[i]`` (PDF p. 8).
DAILY_RUN_SLOT_START_HOURS: tuple[int, ...] = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 0)


class ReadingState(StrEnum):
    """State of a typed reading."""

    AVAILABLE = "available"
    MISSING = "missing"
    INVALID = "invalid"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


class KeyType(StrEnum):
    """Wire value type of a documented key."""

    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    STRING = "string"


class Normalization(StrEnum):
    """Whether a raw value may be normalized to its documented unit."""

    IDENTITY = "identity"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Key(Generic[T]):
    """Immutable descriptor for one documented MSR2 local key.

    ``sentinel`` is a raw wire token that means the quantity is not
    provided (e.g. ``"127"`` on temperature sensors whose probe is not
    connected); such readings become ``UNAVAILABLE``.
    """

    key: str
    group: str
    name: str
    type: KeyType
    documented_unit: str | None
    normalized_unit: str | None
    normalization: Normalization
    notes: str
    source: str
    sentinel: str | None


@dataclass(frozen=True, slots=True)
class Reading(Generic[T]):
    """One typed device value together with its raw wire text.

    ``value`` is ``None`` unless ``state`` is ``AVAILABLE``; ``raw`` is the
    exact (whitespace-trimmed) wire text and is ``None`` only when the key
    was absent from the response.
    """

    raw: str | None
    value: T | None
    state: ReadingState

    @classmethod
    def from_raw(cls, key: Key[T], raw: str | None) -> Reading[T]:
        """Build a reading from the wire text of ``key``.

        Missing keys become ``MISSING``; malformed or empty tokens become
        ``INVALID`` with the raw text preserved; the key's sentinel token
        becomes ``UNAVAILABLE``; well-formed tokens of quantities whose
        scale is unverified become ``UNVERIFIED``. In every non-``MISSING``
        state the raw text is preserved, and ``value`` is only set for
        ``AVAILABLE`` so no unscaled number is presented as a physical
        quantity.
        """
        if raw is None:
            return cls(None, None, ReadingState.MISSING)
        if raw == "":
            return cls(raw, None, ReadingState.INVALID)
        if key.sentinel is not None and raw == key.sentinel:
            return cls(raw, None, ReadingState.UNAVAILABLE)
        if key.type is KeyType.STRING:
            return cls(raw, raw, ReadingState.AVAILABLE)
        if key.type is KeyType.BOOLEAN:
            # Wire format is true/false; 0/1 is accepted for robustness
            # against other firmware.
            if raw in ("0", "false"):
                return cls(raw, False, ReadingState.AVAILABLE)
            if raw in ("1", "true"):
                return cls(raw, True, ReadingState.AVAILABLE)
            return cls(raw, None, ReadingState.INVALID)
        if key.type is KeyType.INTEGER:
            if _INTEGER_TOKEN.match(raw) is None:
                # The Dachsportal serializes integers in exponent notation
                # (observed: ulInbetriebnahmedatum as "3.628008E+8") and its
                # JSON numbers may arrive as integral decimals ("362800800.0").
                # Accept a numeric token whose value is exactly integral.
                if (
                    _DECIMAL_TOKEN.match(raw) is None
                    and _EXPONENT_TOKEN.match(raw) is None
                ):
                    return cls(raw, None, ReadingState.INVALID)
                value = Decimal(raw)
                if value != value.to_integral_value():
                    return cls(raw, None, ReadingState.INVALID)
                return cls(raw, int(value), ReadingState.AVAILABLE)
            if key.normalization is Normalization.UNVERIFIED:
                return cls(raw, None, ReadingState.UNVERIFIED)
            return cls(raw, int(raw), ReadingState.AVAILABLE)
        assert key.type is KeyType.DECIMAL
        if _DECIMAL_TOKEN.match(raw) is None:
            return cls(raw, None, ReadingState.INVALID)
        if key.normalization is Normalization.UNVERIFIED:
            return cls(raw, None, ReadingState.UNVERIFIED)
        return cls(raw, Decimal(raw), ReadingState.AVAILABLE)


@dataclass(frozen=True, slots=True)
class OperatingData:
    """Operating counters and demand/release flags (``Hka_Bd.``)."""

    requested_modules: Reading[int]
    power_request_mask: Reading[int]
    power_release_mask: Reading[int]
    fault_raw: Reading[int]
    warning_raw: Reading[int]
    power_requested: Reading[bool]
    request_mask: Reading[int]
    release_mask: Reading[int]
    electrical_energy: Reading[Decimal]
    thermal_energy: Reading[Decimal]
    condenser_energy: Reading[Decimal]
    runtime: Reading[Decimal]
    starts: Reading[int]


@dataclass(frozen=True, slots=True)
class IdentityData:
    """Device identity (``Hka_Bd_Stat.``)."""

    serial_number: Reading[str]
    part_number: Reading[str]
    commissioned_epoch_seconds: Reading[int]


@dataclass(frozen=True, slots=True)
class YearEndData:
    """Counters frozen at 31.12. of the previous year (``BD3112.``)."""

    runtime: Reading[Decimal]
    starts: Reading[int]
    electrical_energy: Reading[Decimal]
    thermal_energy: Reading[Decimal]
    condenser_energy: Reading[Decimal]
    hot_water_volume: Reading[Decimal]


@dataclass(frozen=True, slots=True)
class SecondGeneratorData:
    """Second heat generator / SEplus burner (``Brenner_Bd.``)."""

    status_raw: Reading[int]
    warning_raw: Reading[int]
    request_mask: Reading[int]
    release_mask: Reading[int]
    starts: Reading[int]
    runtime: Reading[Decimal]


@dataclass(frozen=True, slots=True)
class HydraulicsData:
    """Unresolved hydraulic configuration codes (``Hka_Ew.HydraulikNr.``)."""

    storage_type_raw: Reading[int]
    hot_water_type_raw: Reading[int]
    second_generator_type_raw: Reading[int]
    multi_module_type_raw: Reading[int]


@dataclass(frozen=True, slots=True)
class TemperaturesData:
    """Measured temperatures (``Hka_Mw1.Temp.``), scale unverified."""

    exhaust: Reading[Decimal]
    engine_exhaust: Reading[Decimal]
    enclosure: Reading[Decimal]
    outdoor: Reading[Decimal]
    module_release: Reading[Decimal]
    sensor_1: Reading[Decimal]
    sensor_2: Reading[Decimal]
    generator_inlet: Reading[Decimal]
    engine_coolant: Reading[Decimal]
    controller: Reading[Decimal]
    return_flow: Reading[Decimal]
    supply_flow: Reading[Decimal]
    sensor_3: Reading[Decimal]
    sensor_4: Reading[Decimal]
    heating_circuit_1_supply: Reading[Decimal]
    heating_circuit_2_supply: Reading[Decimal]
    hot_water: Reading[Decimal]


@dataclass(frozen=True, slots=True)
class TemperatureTargetsData:
    """Controller setpoints (``Hka_Mw1.Solltemp.``), scale unverified."""

    return_flow: Reading[Decimal]
    supply_flow: Reading[Decimal]


@dataclass(frozen=True, slots=True)
class ActuatorsData:
    """Actuator states (``Hka_Mw1.Aktor.``)."""

    hot_water_pump: Reading[Decimal]
    alternate_heat_enabled: Reading[bool]
    mixer_1_open: Reading[bool]
    mixer_1_close: Reading[bool]
    mixer_2_open: Reading[bool]
    mixer_2_close: Reading[bool]
    programmable_output_1: Reading[bool]
    programmable_output_2: Reading[bool]
    programmable_output_3: Reading[bool]
    fault_relay: Reading[bool]
    heating_circuit_1_pump: Reading[bool]
    heating_circuit_2_pump: Reading[bool]
    cooling_pump: Reading[bool]
    prepressure_pump: Reading[bool]
    circulation_pump: Reading[bool]
    maintenance_relay: Reading[bool]


@dataclass(frozen=True, slots=True)
class MeasurementsData:
    """Instantaneous measurements (``Hka_Mw1.``)."""

    active_power: Reading[Decimal]
    runtime_since_start: Reading[Decimal]
    engine_speed: Reading[int]


@dataclass(frozen=True, slots=True)
class ModuleData:
    """One module of a multi-module plant (``Mm[i].ModulSteuerung.``)."""

    running: Reading[bool]
    available: Reading[bool]


@dataclass(frozen=True, slots=True)
class ModuleStatisticsData:
    """Runtime/start extremes across modules (``Mm_MinMax.``)."""

    max_maintenance_module: Reading[int]
    min_maintenance_module: Reading[int]
    max_maintenance_runtime: Reading[int]
    min_maintenance_runtime: Reading[int]
    max_runtime_module: Reading[int]
    max_runtime: Reading[Decimal]
    min_runtime_module: Reading[int]
    min_runtime: Reading[Decimal]
    max_starts_module: Reading[int]
    max_starts: Reading[int]
    min_starts_module: Reading[int]
    min_starts: Reading[int]


@dataclass(frozen=True, slots=True)
class MaintenanceData:
    """Maintenance status (``Wartung_Cache.``)."""

    due: Reading[bool]
    last_runtime: Reading[int]
    last_epoch_seconds: Reading[int]
    interval: Reading[int]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """All documented local readings of one device, plus the raw wire map.

    ``daily_run`` has 12 slots in wire order (slot start hours in
    :data:`DAILY_RUN_SLOT_START_HOURS`); ``modules`` has 10 zero-based wire
    slots. Optional equipment that did not answer stays ``MISSING``.
    """

    operating: OperatingData
    identity: IdentityData
    year_end: YearEndData
    second_generator: SecondGeneratorData
    hydraulics: HydraulicsData
    temperatures: TemperaturesData
    temperature_targets: TemperatureTargetsData
    actuators: ActuatorsData
    measurements: MeasurementsData
    daily_run: tuple[Reading[int], ...]
    modules: tuple[ModuleData, ...]
    module_statistics: ModuleStatisticsData
    maintenance: MaintenanceData
    raw: Mapping[str, str]

    @classmethod
    def from_raw(cls, raw: Mapping[str, str]) -> Snapshot:
        """Build a snapshot from received ``key=value`` wire pairs."""
        from .keys import LOCAL_KEYS

        def reading(wire_key: str) -> Reading[Any]:
            return Reading.from_raw(LOCAL_KEYS[wire_key], raw.get(wire_key))

        return cls(
            operating=OperatingData(
                requested_modules=reading("Hka_Bd.Anforderung.ModulAnzahl"),
                power_request_mask=reading("Hka_Bd.Anforderung.UStromF_Anf.bFlagSF"),
                power_release_mask=reading("Hka_Bd.UStromF_Frei.bFreigabe"),
                fault_raw=reading("Hka_Bd.bStoerung"),
                warning_raw=reading("Hka_Bd.bWarnung"),
                power_requested=reading("Hka_Bd.UHka_Anf.Anforderung.fStrom"),
                request_mask=reading("Hka_Bd.UHka_Anf.usAnforderung"),
                release_mask=reading("Hka_Bd.UHka_Frei.usFreigabe"),
                electrical_energy=reading("Hka_Bd.ulArbeitElektr"),
                thermal_energy=reading("Hka_Bd.ulArbeitThermHka"),
                condenser_energy=reading("Hka_Bd.ulArbeitThermKon"),
                runtime=reading("Hka_Bd.ulBetriebssekunden"),
                starts=reading("Hka_Bd.ulAnzahlStarts"),
            ),
            identity=IdentityData(
                serial_number=reading("Hka_Bd_Stat.uchSeriennummer"),
                part_number=reading("Hka_Bd_Stat.uchTeilenummer"),
                commissioned_epoch_seconds=reading("Hka_Bd_Stat.ulInbetriebnahmedatum"),
            ),
            year_end=YearEndData(
                runtime=reading("BD3112.Hka_Bd.ulBetriebssekunden"),
                starts=reading("BD3112.Hka_Bd.ulAnzahlStarts"),
                electrical_energy=reading("BD3112.Hka_Bd.ulArbeitElektr"),
                thermal_energy=reading("BD3112.Hka_Bd.ulArbeitThermHka"),
                condenser_energy=reading("BD3112.Hka_Bd.ulArbeitThermKon"),
                hot_water_volume=reading("BD3112.Ww_Bd.ulWwMengepA"),
            ),
            second_generator=SecondGeneratorData(
                status_raw=reading("Brenner_Bd.bIstStatus"),
                warning_raw=reading("Brenner_Bd.bWarnung"),
                request_mask=reading("Brenner_Bd.UBrenner_Anf.usAnforderung"),
                release_mask=reading("Brenner_Bd.UBrenner_Frei.bFreigabe"),
                starts=reading("Brenner_Bd.ulAnzahlStarts"),
                runtime=reading("Brenner_Bd.ulBetriebssekunden"),
            ),
            hydraulics=HydraulicsData(
                storage_type_raw=reading("Hka_Ew.HydraulikNr.bSpeicherArt"),
                hot_water_type_raw=reading("Hka_Ew.HydraulikNr.bWW_Art"),
                second_generator_type_raw=reading("Hka_Ew.HydraulikNr.b2_Waermeerzeuger"),
                multi_module_type_raw=reading("Hka_Ew.HydraulikNr.bMehrmodul"),
            ),
            temperatures=TemperaturesData(
                exhaust=reading("Hka_Mw1.Temp.sAbgasHKA"),
                engine_exhaust=reading("Hka_Mw1.Temp.sAbgasMotor"),
                enclosure=reading("Hka_Mw1.Temp.sKapsel"),
                outdoor=reading("Hka_Mw1.Temp.sbAussen"),
                module_release=reading("Hka_Mw1.Temp.sbFreigabeModul"),
                sensor_1=reading("Hka_Mw1.Temp.sbFuehler1"),
                sensor_2=reading("Hka_Mw1.Temp.sbFuehler2"),
                generator_inlet=reading("Hka_Mw1.Temp.sbGen"),
                engine_coolant=reading("Hka_Mw1.Temp.sbMotor"),
                controller=reading("Hka_Mw1.Temp.sbRegler"),
                return_flow=reading("Hka_Mw1.Temp.sbRuecklauf"),
                supply_flow=reading("Hka_Mw1.Temp.sbVorlauf"),
                sensor_3=reading("Hka_Mw1.Temp.sbZS_Fuehler3"),
                sensor_4=reading("Hka_Mw1.Temp.sbZS_Fuehler4"),
                heating_circuit_1_supply=reading("Hka_Mw1.Temp.sbZS_Vorlauf1"),
                heating_circuit_2_supply=reading("Hka_Mw1.Temp.sbZS_Vorlauf2"),
                hot_water=reading("Hka_Mw1.Temp.sbZS_Warmwasser"),
            ),
            temperature_targets=TemperatureTargetsData(
                return_flow=reading("Hka_Mw1.Solltemp.sbRuecklauf"),
                supply_flow=reading("Hka_Mw1.Solltemp.sbVorlauf"),
            ),
            actuators=ActuatorsData(
                hot_water_pump=reading("Hka_Mw1.Aktor.bWwPumpe"),
                alternate_heat_enabled=reading("Hka_Mw1.Aktor.fFreiAltWaerm"),
                mixer_1_open=reading("Hka_Mw1.Aktor.fMischer1Auf"),
                mixer_1_close=reading("Hka_Mw1.Aktor.fMischer1Zu"),
                mixer_2_open=reading("Hka_Mw1.Aktor.fMischer2Auf"),
                mixer_2_close=reading("Hka_Mw1.Aktor.fMischer2Zu"),
                programmable_output_1=reading("Hka_Mw1.Aktor.fProgAus1"),
                programmable_output_2=reading("Hka_Mw1.Aktor.fProgAus2"),
                programmable_output_3=reading("Hka_Mw1.Aktor.fProgAus3"),
                fault_relay=reading("Hka_Mw1.Aktor.fStoerung"),
                heating_circuit_1_pump=reading("Hka_Mw1.Aktor.fUPHeizkreis1"),
                heating_circuit_2_pump=reading("Hka_Mw1.Aktor.fUPHeizkreis2"),
                cooling_pump=reading("Hka_Mw1.Aktor.fUPKuehlung"),
                prepressure_pump=reading("Hka_Mw1.Aktor.fUPVordruck"),
                circulation_pump=reading("Hka_Mw1.Aktor.fUPZirkulation"),
                maintenance_relay=reading("Hka_Mw1.Aktor.fWartung"),
            ),
            measurements=MeasurementsData(
                active_power=reading("Hka_Mw1.sWirkleistung"),
                runtime_since_start=reading("Hka_Mw1.ulMotorlaufsekunden"),
                engine_speed=reading("Hka_Mw1.usDrehzahl"),
            ),
            daily_run=tuple(
                reading(f"Laufraster15Min_aktTag.bDoppelstunde[{slot}]")
                for slot in range(len(DAILY_RUN_SLOT_START_HOURS))
            ),
            modules=tuple(
                ModuleData(
                    running=reading(f"Mm[{index}].ModulSteuerung.fModulLaeuft"),
                    available=reading(f"Mm[{index}].ModulSteuerung.fModulVerfuegbar"),
                )
                for index in range(10)
            ),
            module_statistics=ModuleStatisticsData(
                max_maintenance_module=reading("Mm_MinMax.bModulBhMaxWart"),
                min_maintenance_module=reading("Mm_MinMax.bModulBhMinWart"),
                max_maintenance_runtime=reading("Mm_MinMax.sBhMaxWart"),
                min_maintenance_runtime=reading("Mm_MinMax.sBhMinWart"),
                max_runtime_module=reading("Mm_MinMax.ModulBhMax.bModulNr"),
                max_runtime=reading("Mm_MinMax.ModulBhMax.ulWert"),
                min_runtime_module=reading("Mm_MinMax.ModulBhMin.bModulNr"),
                min_runtime=reading("Mm_MinMax.ModulBhMin.ulWert"),
                max_starts_module=reading("Mm_MinMax.ModulStartMax.bModulNr"),
                max_starts=reading("Mm_MinMax.ModulStartMax.ulWert"),
                min_starts_module=reading("Mm_MinMax.ModulStartMin.bModulNr"),
                min_starts=reading("Mm_MinMax.ModulStartMin.ulWert"),
            ),
            maintenance=MaintenanceData(
                due=reading("Wartung_Cache.fStehtAn"),
                last_runtime=reading("Wartung_Cache.ulBetriebssekundenBei"),
                last_epoch_seconds=reading("Wartung_Cache.ulZeitstempel"),
                interval=reading("Wartung_Cache.usIntervall"),
            ),
            raw=MappingProxyType(dict(raw)),
        )
