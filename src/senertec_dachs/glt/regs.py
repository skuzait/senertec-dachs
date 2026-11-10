"""Register catalogue for the Dachs Gen2 GLT (MSR3) Modbus interface.

Every register documented in *Beschreibung der GLT-Schnittstelle Dachs
Gen2* — SenerTec ``8098.119.000.02``, version 02, 19.07.2021. The
interface is Modbus TCP (port 502, big-endian byte order, function
codes 0x04/0x06, recommended request interval 1000 ms or longer, §4.3).

Numbering conventions per the manual:

* Addresses are ``Read Input Register`` (FC 0x04) addresses for the
  8000-area and single-register writes (FC 0x06) for the 8300-area.
* ``UNSIGNED8`` values live in the low byte of the 16-bit register
  (the ioBroker community map reads the same registers live with the
  ``uint8be`` type, which selects the lower half of the register).
* ``UNSIGNED32`` occupies two consecutive registers, high word first.
* ``VisibleString[20]`` spans ten registers as UTF-8 with NUL padding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Mapping


class GltKind(Enum):
    """Register data types of the GLT spec (§6.1)."""

    U16 = "UNSIGNED16"
    I16 = "INTEGER16"
    U32 = "UNSIGNED32"
    U8 = "UNSIGNED8"
    STR20 = "VisibleString[20]"


#: Single contiguous documented read area 8000..8083 (84 registers).
READ_BASE = 8000
READ_END = 8083  # inclusive

#: Write area 8300..8302 — per spec write access (FC 0x06) only.
WRITE_AREA = frozenset({8300, 8301, 8302})


@dataclass(frozen=True, slots=True)
class RegSpec:
    """One documented register with its unit, scale and provenance.

    ``factor`` is the value resolution: the decoded integer is
    multiplied by it to yield the physical quantity in ``unit``
    (identity when ``None``). ``param`` is the display-side parameter
    code (MM000 and friends), ``note`` the semantic clarification.
    """

    name: str
    address: int
    kind: GltKind
    unit: str = ""
    factor: Decimal | None = None
    param: str | None = None
    enum: Mapping[int, str] | None = field(default=None)
    msr2_key: str | None = None
    note: str = ""
    source: str = ""


def _enum(*pairs: tuple[int, str]) -> Mapping[int, str]:
    return dict(pairs)


# -- identity profile (§6.2.1) ------------------------------------------------

GLT_VERSION = RegSpec(
    name="glt_version",
    address=8000,
    kind=GltKind.U16,
    note="MSB = main version, LSB = sub version (NM107)",
    source="8098.119.000.02 §6.2.1",
)
DEVICE_TYPE = RegSpec(
    name="device_type",
    address=8001,
    kind=GltKind.U16,
    note="MSB product group 26 (engine CHP); LSB 1 = Dachs 5.5, 2 = Dachs 2.9",
    source="8098.119.000.02 §6.2.1",
)
SERIAL = RegSpec(
    name="serial_number",
    address=8002,
    kind=GltKind.STR20,
    msr2_key="Hka_Bd_Stat.uchSeriennummer",
    note="individual serial number, 20-byte UTF-8 with NUL padding; same "
         "key identity as the MSR2 serial",
    source="8098.119.000.02 §6.2.1",
)
RATED_POWER = RegSpec(
    name="rated_power_watts",
    address=8012,
    kind=GltKind.U16,
    unit="W",
    factor=Decimal(1),
    param="MP004",
    note="nominal power of the module",
    source="8098.119.000.02 §6.2.1",
)

# -- status of the CHP unit (§6.2.2) ------------------------------------------

PLANT_STATUS = RegSpec(
    name="plant_status_raw",
    address=8013,
    kind=GltKind.U8,
    enum=_enum((0, "Aus"), (1, "Standby"), (2, "Läuft"), (3, "Wartezustand"), (4, "Fehler")),
    note="operating status of the plant",
    source="8098.119.000.02 §6.2.2",
)
ELECTRIC_POWER = RegSpec(
    name="electric_output_kw",
    address=8014,
    kind=GltKind.I16,
    unit="kW",
    factor=Decimal("0.1"),
    msr2_key="Hka_Mw1.sWirkleistung",
    note="current electrical output; same quantity as the MSR2 power key",
    source="8098.119.000.02 §6.2.2",
)
REQUEST_KIND = RegSpec(
    name="request_kind_raw",
    address=8015,
    kind=GltKind.U8,
    enum=_enum((0, "Keine"), (1, "Mindestlaufzeit"), (2, "Notbetrieb"),
               (3, "Stromanforderung"), (4, "Wärmeanforderung"),
               (5, "TWW Anforderung"), (6, "Kaminkehrermodus")),
    note="kind of the current demand",
    source="8098.119.000.02 §6.2.2",
)
RUNTIME_SINCE_START = RegSpec(
    name="runtime_since_start_hours",
    address=8016,
    kind=GltKind.U8,
    unit="h",
    factor=Decimal("0.1"),
    param="MM004",
    msr2_key="Hka_Mw1.ulMotorlaufsekunden",
    note="runtime since the last start; same MM004 identity as cloud v2 "
         "(the MSR2 key carries hours despite its name)",
    source="8098.119.000.02 §6.2.2",
)
SHUTDOWN_REASON = RegSpec(
    name="shutdown_reason_raw",
    address=8017,
    kind=GltKind.U8,
    enum=_enum((0, "Nicht definiert"), (1, "Manuell"), (2, "Keine Anforderung"),
               (3, "Fehler"), (4, "Eintritttemp. zu hoch"), (5, "24h Abschaltung"),
               (6, "Takten/Stillstand"), (7, "Freigabe Netz"), (8, "Freigabe Uhr"),
               (9, "Freigabe extern"), (10, "NA-Schutz Fehler"),
               (11, "ReleaseBuildingManagementSystem")),
    note="reason of the last shutdown",
    source="8098.119.000.02 §6.2.2",
)
HEATING_PUMP_STATUS = RegSpec(
    name="heating_water_pump_raw",
    address=8018,
    kind=GltKind.U8,
    param="MM053",
    enum=_enum((0, "Aus"), (1, "Ein")),
    note="heating water pump status",
    source="8098.119.000.02 §6.2.2",
)
OUTLET_TEMP = RegSpec(
    name="outlet_temp_c",
    address=8019,
    kind=GltKind.U16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM010",
    msr2_key="Hka_Mw1.Temp.sbVorlauf",
    note="CHP outlet temperature; cloud v2 maps the same quantity (MM025 "
         "there) to the MSR2 supply key",
    source="8098.119.000.02 §6.2.2",
)
INLET_TEMP = RegSpec(
    name="inlet_temp_c",
    address=8020,
    kind=GltKind.U16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM011",
    msr2_key="Hka_Mw1.Temp.sbGen",
    note="CHP inlet temperature; same MM011 identity as cloud v2",
    source="8098.119.000.02 §6.2.2",
)

# -- configuration of the unit (§6.2.3) ----------------------------------------

LEADING_VARIABLE = RegSpec(
    name="leading_variable_raw",
    address=8021,
    kind=GltKind.U8,
    param="MP006",
    enum=_enum((0, "Wärme"), (1, "Strom"), (2, "Wärme und Strom")),
    source="8098.119.000.02 §6.2.3",
)
MIN_RUNTIME = RegSpec(
    name="min_runtime_minutes",
    address=8022,
    kind=GltKind.U8,
    unit="min",
    param="MP007",
    source="8098.119.000.02 §6.2.3",
)
MAX_INLET_TEMP = RegSpec(
    name="max_inlet_temp_c",
    address=8023,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MP011",
    source="8098.119.000.02 §6.2.3",
)
MODULATION = RegSpec(
    name="modulation_raw",
    address=8024,
    kind=GltKind.U8,
    param="MP008",
    enum=_enum((0, "Aus"), (1, "Ein")),
    source="8098.119.000.02 §6.2.3",
)
POWER_LEVEL_NO_MODULATION = RegSpec(
    name="power_level_without_modulation",
    address=8025,
    kind=GltKind.U8,
    param="MP009",
    source="8098.119.000.02 §6.2.3",
)
MODULE_TYPE = RegSpec(
    name="module_type_raw",
    address=8026,
    kind=GltKind.U8,
    param="NP210",
    enum=_enum((0, "Undefined"), (1, "L"), (2, "M")),
    source="8098.119.000.02 §6.2.3",
)

# -- operating data (§6.2.4) ----------------------------------------------------

TOTAL_HOURS = RegSpec(
    name="total_hours",
    address=8027,
    kind=GltKind.U32,
    unit="h",
    factor=Decimal(1),
    param="MC000",
    msr2_key="Hka_Bd.ulBetriebssekunden",
    note="total operating hours; same MC000 identity as cloud v2 (MSR2 key "
         "carries hours despite its name)",
    source="8098.119.000.02 §6.2.4",
)
STARTS = RegSpec(
    name="starts",
    address=8029,
    kind=GltKind.U32,
    param="MC001",
    msr2_key="Hka_Bd.ulAnzahlStarts",
    note="number of starts; same MC001 identity as cloud v2",
    source="8098.119.000.02 §6.2.4",
)
ELECTRIC_ENERGY = RegSpec(
    name="electric_energy_kwh",
    address=8031,
    kind=GltKind.U32,
    unit="kWh",
    factor=Decimal("0.1"),
    param="MC002",
    msr2_key="Hka_Bd.ulArbeitElektr",
    note="electrical energy; same MC002 identity as cloud v2 (GLT "
         "resolution 0.1 kWh is coarser than the cloud's 0.001 kWh)",
    source="8098.119.000.02 §6.2.4",
)
THERMAL_ENERGY = RegSpec(
    name="thermal_energy_kwh",
    address=8033,
    kind=GltKind.U32,
    unit="kWh",
    factor=Decimal("0.1"),
    param="MC003",
    msr2_key="Hka_Bd.ulArbeitThermHka",
    note="thermal energy; same MC003 identity as cloud v2",
    source="8098.119.000.02 §6.2.4",
)
HOURS_LEVEL1 = RegSpec(
    name="hours_level_1",
    address=8035,
    kind=GltKind.U32,
    unit="h",
    factor=Decimal(1),
    param="MC005",
    source="8098.119.000.02 §6.2.4",
)
HOURS_LEVEL2 = RegSpec(
    name="hours_level_2",
    address=8037,
    kind=GltKind.U32,
    unit="h",
    factor=Decimal(1),
    param="MC006",
    source="8098.119.000.02 §6.2.4",
)
HOURS_LEVEL3 = RegSpec(
    name="hours_level_3",
    address=8039,
    kind=GltKind.U32,
    unit="h",
    factor=Decimal(1),
    param="MC007",
    source="8098.119.000.02 §6.2.4",
)

# -- system data (§6.2.5) --------------------------------------------------------

OUTDOOR_TEMP = RegSpec(
    name="outdoor_temp_c",
    address=8041,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="AM027",
    msr2_key="Hka_Mw1.Temp.sbAussen",
    note="outdoor temperature; same AM027 identity as cloud v2",
    source="8098.119.000.02 §6.2.5",
)
BUFFER_TEMP_1 = RegSpec(
    name="buffer_temp_1_c",
    address=8042,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="BM001",
    source="8098.119.000.02 §6.2.5",
)
BUFFER_TEMP_2 = RegSpec(
    name="buffer_temp_2_c",
    address=8043,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="BM002",
    source="8098.119.000.02 §6.2.5",
)
BUFFER_TEMP_3 = RegSpec(
    name="buffer_temp_3_c",
    address=8044,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="BM003",
    source="8098.119.000.02 §6.2.5",
)
BUFFER_TEMP_4 = RegSpec(
    name="buffer_temp_4_c",
    address=8045,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="BM004",
    source="8098.119.000.02 §6.2.5",
)
TEMP_SENSOR_5 = RegSpec(
    name="temp_sensor_5_c",
    address=8052,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM105",
    source="8098.119.000.02 §6.2.5",
)
TEMP_SENSOR_6 = RegSpec(
    name="temp_sensor_6_c",
    address=8053,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM106",
    source="8098.119.000.02 §6.2.5",
)
TEMP_SENSOR_7 = RegSpec(
    name="temp_sensor_7_c",
    address=8054,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM107",
    source="8098.119.000.02 §6.2.5",
)
TEMP_SENSOR_8 = RegSpec(
    name="temp_sensor_8_c",
    address=8055,
    kind=GltKind.I16,
    unit="°C",
    factor=Decimal("0.1"),
    param="MM108",
    source="8098.119.000.02 §6.2.5",
)
DISCHARGE_POWER = RegSpec(
    name="discharge_power_percent",
    address=8056,
    kind=GltKind.U16,
    unit="%",
    factor=Decimal(1),
    param="MM076",
    note="current discharge rating of the buffer",
    source="8098.119.000.02 §6.2.5",
)

# -- plant configuration (§6.2.6) and multi-module plant (§6.2.7) -----------------

BUFFER_TYPE = RegSpec(
    name="buffer_type_raw",
    address=8057,
    kind=GltKind.U8,
    param="MP038",
    enum=_enum((0, "Nicht vorhanden"), (1, "Eigenprodukt"),
               (2, "Eigenprodukt mit WT"), (3, "Fremdprodukt")),
    source="8098.119.000.02 §6.2.6",
)
BUFFER_VOLUME = RegSpec(
    name="buffer_volume_liters",
    address=8058,
    kind=GltKind.U16,
    unit="l",
    factor=Decimal(1),
    param="BP033",
    source="8098.119.000.02 §6.2.6",
)
BUFFER_SENSORS = RegSpec(
    name="buffer_sensor_type_raw",
    address=8059,
    kind=GltKind.U8,
    param="BP001",
    enum=_enum((0, "deaktiviert"), (1, "Ein Sensor"), (2, "Zwei Sensoren"),
               (3, "Drei Sensoren"), (4, "Vier Sensoren")),
    source="8098.119.000.02 §6.2.6",
)
BUFFER_POS_1 = RegSpec(
    name="buffer_sensor_pos_1_percent",
    address=8060,
    kind=GltKind.U8,
    unit="%",
    factor=Decimal(1),
    param="BP034",
    source="8098.119.000.02 §6.2.6",
)
BUFFER_POS_2 = RegSpec(
    name="buffer_sensor_pos_2_percent",
    address=8061,
    kind=GltKind.U8,
    unit="%",
    factor=Decimal(1),
    param="BP035",
    source="8098.119.000.02 §6.2.6",
)
BUFFER_POS_3 = RegSpec(
    name="buffer_sensor_pos_3_percent",
    address=8062,
    kind=GltKind.U8,
    unit="%",
    factor=Decimal(1),
    param="BP036",
    source="8098.119.000.02 §6.2.6",
)
BUFFER_POS_4 = RegSpec(
    name="buffer_sensor_pos_4_percent",
    address=8063,
    kind=GltKind.U8,
    unit="%",
    factor=Decimal(1),
    param="BP037",
    source="8098.119.000.02 §6.2.6",
)
HEAT_STORAGE = RegSpec(
    name="heat_storage_raw",
    address=8064,
    kind=GltKind.U8,
    param="BP040",
    enum=_enum((0, "Minimal"), (1, "Eco"), (2, "Komfort"), (3, "Maximum"),
               (4, "Automatisch")),
    source="8098.119.000.02 §6.2.6",
)
BUFFER_DISCHARGE_ENABLED = RegSpec(
    name="buffer_discharge_raw",
    address=8065,
    kind=GltKind.U8,
    param="MP040",
    enum=_enum((0, "Aus"), (1, "Ein")),
    source="8098.119.000.02 §6.2.6",
)
MULTI_POWER = RegSpec(
    name="multi_power_kw",
    address=8066,
    kind=GltKind.U16,
    unit="kW",
    factor=Decimal("0.1"),
    note="active power of the multi-module system",
    source="8098.119.000.02 §6.2.7",
)
MULTI_MODULES_DETECTED = RegSpec(
    name="multi_modules_detected",
    address=8067,
    kind=GltKind.U8,
    source="8098.119.000.02 §6.2.7",
)
MULTI_MODULES_AVAILABLE = RegSpec(
    name="multi_modules_available",
    address=8068,
    kind=GltKind.U8,
    source="8098.119.000.02 §6.2.7",
)
MULTI_MODULES_REQUESTED = RegSpec(
    name="multi_modules_requested",
    address=8069,
    kind=GltKind.U8,
    msr2_key="Hka_Bd.Anforderung.ModulAnzahl",
    note="requested modules of the multi-module system; same quantity as "
         "the MSR2 demand key",
    source="8098.119.000.02 §6.2.7",
)
MULTI_MODULES_RUNNING = RegSpec(
    name="multi_modules_running",
    address=8070,
    kind=GltKind.U8,
    source="8098.119.000.02 §6.2.7",
)
MULTI_MODULE_COUNT = RegSpec(
    name="multi_module_count",
    address=8071,
    kind=GltKind.U8,
    param="NP211",
    source="8098.119.000.02 §6.2.7",
)
MULTI_RATED_POWER = RegSpec(
    name="multi_rated_power_kw",
    address=8072,
    kind=GltKind.U16,
    unit="kW",
    factor=Decimal("0.1"),
    source="8098.119.000.02 §6.2.7",
)

# -- second heat generator (§6.2.8) -----------------------------------------------

SECOND_GEN_STATUS = RegSpec(
    name="second_generator_status_raw",
    address=8073,
    kind=GltKind.U8,
    param="NM005",
    enum=_enum((0, "Aus"), (1, "Ein")),
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_TARGET_STATUS = RegSpec(
    name="second_generator_target_raw",
    address=8074,
    kind=GltKind.U8,
    param="NM006",
    enum=_enum((0, "Aus"), (1, "Ein")),
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_STARTS = RegSpec(
    name="second_generator_starts",
    address=8075,
    kind=GltKind.U32,
    param="NC010",
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_HOURS = RegSpec(
    name="second_generator_hours",
    address=8077,
    kind=GltKind.U32,
    unit="h",
    factor=Decimal(1),
    param="NC011",
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_AVAILABLE = RegSpec(
    name="second_generator_available_raw",
    address=8079,
    kind=GltKind.U8,
    param="NP015",
    enum=_enum((0, "Nein"), (1, "Ja")),
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_RELEASED = RegSpec(
    name="second_generator_released_raw",
    address=8080,
    kind=GltKind.U8,
    param="NP018",
    enum=_enum((0, "Nein"), (1, "Ja")),
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_RATED_POWER = RegSpec(
    name="second_generator_rated_kw",
    address=8081,
    kind=GltKind.U16,
    unit="kW",
    factor=Decimal("0.1"),
    param="NP016",
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_MIN_RUNTIME = RegSpec(
    name="second_generator_min_runtime_min",
    address=8082,
    kind=GltKind.U8,
    unit="min",
    param="NP017",
    source="8098.119.000.02 §6.2.8",
)
SECOND_GEN_ON_BUFFER = RegSpec(
    name="second_generator_on_buffer_raw",
    address=8083,
    kind=GltKind.U8,
    param="NP019",
    enum=_enum((0, "Nein"), (1, "Ja")),
    source="8098.119.000.02 §6.2.8",
)

#: All documented read registers, in address order.
GLT_REGS: tuple[RegSpec, ...] = (
    GLT_VERSION, DEVICE_TYPE, SERIAL, RATED_POWER,
    PLANT_STATUS, ELECTRIC_POWER, REQUEST_KIND, RUNTIME_SINCE_START,
    SHUTDOWN_REASON, HEATING_PUMP_STATUS, OUTLET_TEMP, INLET_TEMP,
    LEADING_VARIABLE, MIN_RUNTIME, MAX_INLET_TEMP, MODULATION,
    POWER_LEVEL_NO_MODULATION, MODULE_TYPE,
    TOTAL_HOURS, STARTS, ELECTRIC_ENERGY, THERMAL_ENERGY,
    HOURS_LEVEL1, HOURS_LEVEL2, HOURS_LEVEL3,
    OUTDOOR_TEMP, BUFFER_TEMP_1, BUFFER_TEMP_2, BUFFER_TEMP_3,
    BUFFER_TEMP_4, TEMP_SENSOR_5, TEMP_SENSOR_6, TEMP_SENSOR_7,
    TEMP_SENSOR_8, DISCHARGE_POWER,
    BUFFER_TYPE, BUFFER_VOLUME, BUFFER_SENSORS, BUFFER_POS_1, BUFFER_POS_2,
    BUFFER_POS_3, BUFFER_POS_4, HEAT_STORAGE, BUFFER_DISCHARGE_ENABLED,
    MULTI_POWER, MULTI_MODULES_DETECTED, MULTI_MODULES_AVAILABLE,
    MULTI_MODULES_REQUESTED, MULTI_MODULES_RUNNING, MULTI_MODULE_COUNT,
    MULTI_RATED_POWER,
    SECOND_GEN_STATUS, SECOND_GEN_TARGET_STATUS, SECOND_GEN_STARTS,
    SECOND_GEN_HOURS, SECOND_GEN_AVAILABLE, SECOND_GEN_RELEASED,
    SECOND_GEN_RATED_POWER, SECOND_GEN_MIN_RUNTIME, SECOND_GEN_ON_BUFFER,
)

GLT_BY_NAME: Mapping[str, RegSpec] = {r.name: r for r in GLT_REGS}

#: Registers documented as "not used" — decoded but not published:
#: 8046/8047 (BM005/006 buffer), 8048..8051 (MM101..104 senses).
UNUSED_ADDRESSES = frozenset({8046, 8047, 8048, 8049, 8050, 8051})
