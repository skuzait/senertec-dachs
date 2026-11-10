"""Datapoint catalogue and first-generation compatibility mapping.

The runtime catalogue is built from two server payloads: ``/rest/info/init``
(all ``metaDataPoints`` with gains, units, enums and the translation
tables) plus the unit payload from ``/rest/canip/{serial}`` (which
boards carry which datapoints). Session-scoped md5 ids rotate, so
nothing here is hardcoded beyond the stable codes the server stores as
``friendlyName`` (``MM000`` style) — the same codes the Dachs Gen2
operating manual (8098.003.000) documents.

The compatibility map (:data:`COMPAT_KEYS`) maps first-generation MSR2
GLT keys to cloud v2 datapoints **only where the physical quantity is
provably the same** — evidenced jointly by the operating manual section
and the server-provided metadata unit/gain. MSR2-only control and
status blocks (module flags, the 15-minute run grid, actuator
bitfields, the MSR2 fault-code namespace, heating-circuit sensor
slots) have no verified counterpart and stay unanswered instead of
being guessed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .protocol import MetaDataPoint, parse_meta_datapoint

_WATT_TO_KILOWATT = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class CompatSpec:
    """One first-generation key backed by a cloud v2 datapoint.

    ``convert`` receives the gain-applied value in the v2 unit and
    returns wire text in the v1-documented unit; ``None`` means the
    units already agree (or are unit-less).
    """

    key: str
    code: str
    board: str
    convert: Callable[[Decimal], str]
    source: str


def _identity(value: Decimal) -> str:
    """Exact decimal text without trailing-zero noise."""
    text = str(value)
    if "." in text:
        stripped = text.rstrip("0").rstrip(".")
        return stripped or "0"
    return text


def _watts_to_kilowatts(value: Decimal) -> str:
    return str((value * _WATT_TO_KILOWATT).normalize())


def _integral(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return str(value)


#: First-generation keys whose v2 meaning is evidenced. Wire semantics of
#: names like ``ulBetriebssekunden`` carry hours despite the name (the
#: catalogue documents this), so runtime datapoints map identity;
#: ``MM000`` is watts while the MSR2 key is kW, hence the exact /1000.
COMPAT_KEYS: tuple[CompatSpec, ...] = (
    CompatSpec(
        "Hka_Bd.ulBetriebssekunden",
        "MC000",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.1.4 (MC000 total operating hours) "
        "+ metadata gain 1 Hours",
    ),
    CompatSpec(
        "Hka_Bd.ulAnzahlStarts",
        "MC001",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.1.5 (MC001 number of starts) + "
        "metadata gain 1",
    ),
    CompatSpec(
        "Hka_Bd.ulArbeitElektr",
        "MC002",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.1.9 (MC002 electrical energy, kWh) "
        "+ metadata gain 0.001 kWh",
    ),
    CompatSpec(
        "Hka_Bd.ulArbeitThermHka",
        "MC003",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.1.10 (MC003 thermal energy, kWh) + "
        "metadata gain 0.001 kWh",
    ),
    CompatSpec(
        "Hka_Mw1.sWirkleistung",
        "MM000",
        "CU-CHP01",
        _watts_to_kilowatts,
        "Gen2 manual 8098.003.000 §8.1.1 (MM000 current electrical "
        "output, watts) + metadata gain 1 W; the MSR2 key is kW and the "
        "conversion is exact /1000",
    ),
    CompatSpec(
        "Hka_Mw1.ulMotorlaufsekunden",
        "MM004",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.1.3 (MM004 runtime since last "
        "start) + metadata gain 0.1 Hours",
    ),
    CompatSpec(
        "Hka_Mw1.usDrehzahl",
        "MM001",
        "CU-CHP01",
        _integral,
        "Gen2 manual 8098.003.000 §8.4 (MM001 engine speed, rpm) + "
        "metadata gain 0.1 Rpm",
    ),
    CompatSpec(
        "Hka_Mw1.Temp.sbAussen",
        "AM027",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §10.4.2 (AM027 outdoor temperature) + "
        "metadata gain 0.01 °C",
    ),
    CompatSpec(
        "Hka_Mw1.Temp.sbGen",
        "MM011",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §11.4.1 (MM011 CHP inlet temperature) "
        "+ metadata gain 0.01 °C; the MSR2 key is the Dachs inlet "
        "temperature",
    ),
    CompatSpec(
        "Hka_Mw1.Temp.sbVorlauf",
        "MM025",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §8.4/§11.4.2 (MM025 CHP outlet "
        "temperature 1) + metadata gain 0.01 °C; the MSR2 key is the "
        "supply temperature",
    ),
    CompatSpec(
        "Wartung_Cache.ulBetriebssekundenBei",
        "MP033",
        "CU-CHP01",
        _identity,
        "Gen2 manual 8098.003.000 §14.2.2 (MP033 operating hours at "
        "last maintenance) + metadata gain 1 Hours",
    ),
)

#: Compatibility map by first-generation key.
COMPAT_BY_KEY: Mapping[str, CompatSpec] = {spec.key: spec for spec in COMPAT_KEYS}

_CATALOGUE_RECORD_FIELDS = frozenset(
    {"id", "code", "gain", "unit", "obd_class", "type_name", "java_type"}
)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Runtime datapoint catalogue for one connected unit."""

    board_to_codes: Mapping[str, Mapping[str, MetaDataPoint]]
    """board name → (stable code → metadata)."""

    code_to_boards: Mapping[str, tuple[str, ...]]
    """stable code → boards that carry it."""

    id_to_point: Mapping[str, MetaDataPoint]
    """session md5 id → metadata (all datapoints of the unit's boards)."""

    def point(self, code: str, board: str | None = None) -> MetaDataPoint:
        """Resolve one datapoint by stable code, optionally per board."""
        candidates = self.code_to_boards.get(code, ())
        if board is not None and board not in candidates:
            raise KeyError(code)
        if board is None:
            if not candidates:
                raise KeyError(code)
            return self.board_to_codes[candidates[0]][code]
        return self.board_to_codes[board][code]


def build_catalogue(
    init_json: Mapping[str, Any], unit_json: Mapping[str, Any]
) -> Catalogue:
    """Resolve the catalogue for one connected unit.

    Single broken metadata entries are skipped; structural breakage (no
    metadata map, no boards) raises :class:`ValueError`.
    """
    metadata = init_json.get("metaDataPoints")
    boards = unit_json.get("boards")
    if not isinstance(metadata, Mapping) or not metadata:
        raise ValueError("init payload has no metaDataPoints block")
    if not isinstance(boards, list) or not boards:
        raise ValueError("unit payload has no boards")

    parsed: dict[str, MetaDataPoint] = {}
    for label, entry in metadata.items():
        try:
            parsed[label] = parse_meta_datapoint(str(label), entry)
        except Exception:  # noqa: BLE001 — one broken point never kills the catalogue
            continue

    board_to_codes: dict[str, dict[str, MetaDataPoint]] = {}
    code_to_boards_builder: dict[str, list[str]] = {}
    id_to_point: dict[str, MetaDataPoint] = {}
    for board in boards:
        if not isinstance(board, Mapping):
            continue
        name = board.get("name")
        if not isinstance(name, str) or not name:
            continue
        mapping: dict[str, MetaDataPoint] = {}
        for attribute in board.get("attributes") or ():
            point = parsed.get(attribute)
            if point is None:
                continue
            display = point.code or point.id
            mapping[display] = point
            code_to_boards_builder.setdefault(display, []).append(name)
        board_to_codes[name] = mapping
        id_to_point.update({point.id: point for point in mapping.values()})

    if not board_to_codes:
        raise ValueError("no board of the unit could be resolved")

    return Catalogue(
        board_to_codes=board_to_codes,
        code_to_boards={
            code: tuple(boards) for code, boards in code_to_boards_builder.items()
        },
        id_to_point=id_to_point,
    )


def compat_resolution(catalogue: Catalogue) -> dict[str, str]:
    """Compatibility keys → session md5 ids for available datapoints."""
    resolved: dict[str, str] = {}
    for key, spec in COMPAT_BY_KEY.items():
        try:
            point = catalogue.point(spec.code, spec.board)
        except KeyError:
            continue
        resolved[key] = point.id
    return resolved
