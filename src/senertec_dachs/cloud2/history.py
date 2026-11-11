"""Typed history aggregates for DachsConnect (cloud v2).

The portal website derives its daily cards ("Produzierter Strom
heute", "Betriebsstunden heute", "Letzte Abschaltgründe") from the
``/rest/charts/{serial}/data`` endpoint. This module turns those raw
chart answers into typed, honestly-unit-ed records.

Field semantics were verified against the production frontend on the
demo unit (96385, November 2026):

- ``ChartEnergyProducing`` rows are per **day** (``{"time": …,
  "format": "d"}``), Wh-scaled (gain 0.001 → kWh) with fields
  ``SelfConsumption``, ``TotalConsumption`` and the grid feed-in. The
  feed-in field was cross-checked against ``ChartElectricityBalance``
  (both carry the same Wh value per day).
- ``ChartElectricityBalance`` carries per-day Wh rows: the EM board
  ``[from grid, to grid]``, the CU board a single field of produced
  heat (matches the portal's "Produzierte Wärme heute" card).
- ``ChartOperatingData`` rows are **hourly, cumulative since the day
  started** (gain 1/3600 → hours): per-load-level runtimes, the
  day's starts, produced heat and producer energy. The daily figure
  is the last row of each day.
- ``ChartChpStatus`` rows are hourly ``[device state, shutdown
  reason]`` pairs; the reason maps through the ``ShutDownReason``
  enum, the state through ``DeviceState`` (both shipped in
  ``/rest/info/init`` translations).

Rows the portal has not answered yet are skipped; every row carries
the timestamp it stands for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

__all__ = [
    "DailyElectric",
    "DailyHeat",
    "DailyRun",
    "ShutdownSample",
    "UnitDetails",
    "daily_electric_rows",
    "daily_heat_rows",
    "daily_run_rows",
    "shutdown_rows",
]


@dataclass(frozen=True)
class DailyElectric:
    """One day of electric energy accounting (per-day chart rows)."""

    day: date
    #: Electric energy consumed on-site that day (CHP + battery + PV).
    self_consumed_kwh: Decimal
    #: Everything consumed that day: grid import plus on-site sources.
    total_consumed_kwh: Decimal
    #: Electric energy fed into the grid that day.
    fed_in_kwh: Decimal
    #: Electric energy imported from the grid that day.
    from_grid_kwh: Decimal
    #: Electric energy exported to the grid that day.
    to_grid_kwh: Decimal


@dataclass(frozen=True)
class DailyHeat:
    """One day of produced heat (CU board balance row)."""

    day: date
    produced_kwh: Decimal


@dataclass(frozen=True)
class DailyRun:
    """One day of CHP runtime, from cumulative hourly rows."""

    day: date
    #: Operating hours that day (cumulative series, last row wins).
    hours: Decimal
    #: Engine starts that day.
    starts: int
    #: Operating hours in load level 2 that day.
    ls2_hours: Decimal
    #: Operating hours in load level 3 that day.
    ls3_hours: Decimal


@dataclass(frozen=True)
class ShutdownSample:
    """One hourly CHP state/reason sample."""

    at: datetime
    #: Device state: translated text when the enum is known, else the raw code.
    state: str | int
    #: Shutdown reason: translated text when the enum is known, else the raw code.
    reason: str | int


@dataclass(frozen=True)
class UnitDetails:
    """Static identity of the connected unit (unit payload snapshot)."""

    serial: str | None
    #: Marketing name, e.g. "Dachs Gen2 F5.5" (``benennung``).
    model: str | None
    #: Product family, e.g. "dachsII" (``productGroup``).
    product_group: str | None
    #: SenerTec article number (``artikelNummer``).
    article_number: str | None
    #: Commissioning instant (``inbetriebnahmeDatum``, epoch ms).
    commissioning_date: datetime | None
    operator_name: str | None
    location_name: str | None
    address: str | None
    postal_code: str | None
    city: str | None
    country_code: int | None
    contact_name: str | None
    phone: str | None
    #: Serial of the leading unit of a cascade (``leitHkaSn``).
    lead_unit_serial: str | None
    module_number: int | None
    #: Service contract exists (``hasVertrag``).
    has_service_contract: bool | None
    #: Active warnings of the unit (``warnings``; the "Warnung steht
    #: an" banner is driven by this list).
    warnings: tuple[str, ...] = ()
    connected: bool = False
    online: bool = False


def _wh_to_kwh(raw: Any) -> Decimal:
    """Charts carry Wh with metadata gain 0.001."""
    if raw is None:
        return Decimal("0")
    return Decimal(str(raw)) * Decimal("0.001")


def _raw_to_hours(raw: Any) -> Decimal:
    """ChartOperatingData rows carry seconds with gain 1/3600."""
    if raw is None:
        return Decimal("0")
    return Decimal(str(raw)) * Decimal("1") / Decimal("3600")


def _row_time(key: Any) -> tuple[int, bool] | None:
    """Row key → ``(epoch seconds, is_daily)``.

    Daily rows key their rows with a JSON string
    (``{"time": 1794308400, "format": "d"}``), hourly rows with the
    plain epoch seconds (``1794308400``). Anything else → ``None``.
    """
    if isinstance(key, str) and key.startswith("{"):
        import json

        try:
            parsed = json.loads(key)
        except ValueError:
            return None
        if isinstance(parsed, Mapping) and isinstance(parsed.get("time"), int):
            return parsed["time"], True
        return None
    try:
        return int(key), False
    except (TypeError, ValueError):
        return None


def _day_of(epoch_seconds: int) -> date:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date()


def _last_row_per_day(
    rows: Mapping[str, Any],
) -> list[tuple[date, list[str]]]:
    """Collapse cumulative hourly rows to the last row of each day."""
    per_day: dict[date, tuple[int, list[str]]] = {}
    for key, values in rows.items():
        stamped = _row_time(key)
        if stamped is None or not isinstance(values, (list, tuple)):
            continue
        epoch, daily = stamped
        day = _day_of(epoch)
        current = per_day.get(day)
        if current is None or epoch >= current[0]:
            per_day[day] = (epoch, [str(v) for v in values])
    return [(day, values) for day, (_, values) in sorted(per_day.items())]


def daily_electric_rows(
    producing: Mapping[str, Any], balance: Mapping[str, Any]
) -> list[DailyElectric]:
    """Parse ``ChartEnergyProducing`` + ``ChartElectricityBalance``.

    ``producing`` fields, positionally: self-consumed, total consumed,
    grid feed-in (Wh). ``balance``: the EM board's row ``[from grid,
    to grid]`` (Wh). Days missing on either side are skipped.
    """
    prod_rows: dict[date, list[str]] = {}
    common = (producing.get("boardMap") or {}).get("common") or producing
    for key, values in (common.get("rows") or {}).items():
        stamped = _row_time(key)
        if stamped is None or not stamped[1]:  # daily rows only
            continue
        if not isinstance(values, (list, tuple)) or len(values) < 3:
            continue
        prod_rows[_day_of(stamped[0])] = [str(v) for v in values]

    em_rows: dict[date, list[str]] = {}
    em = (balance.get("boardMap") or {}).get("EM-CHP01") or {}
    for key, values in (em.get("rows") or {}).items():
        stamped = _row_time(key)
        if stamped is None or not stamped[1]:
            continue
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            continue
        em_rows[_day_of(stamped[0])] = [str(v) for v in values]

    out: list[DailyElectric] = []
    for day in sorted(set(prod_rows) & set(em_rows)):
        produced = prod_rows[day]
        grid = em_rows[day]
        out.append(
            DailyElectric(
                day=day,
                self_consumed_kwh=_wh_to_kwh(produced[0]),
                total_consumed_kwh=_wh_to_kwh(produced[1]),
                fed_in_kwh=_wh_to_kwh(produced[2]),
                from_grid_kwh=_wh_to_kwh(grid[0]),
                to_grid_kwh=_wh_to_kwh(grid[1]),
            )
        )
    return out


def daily_heat_rows(balance: Mapping[str, Any]) -> list[DailyHeat]:
    """Parse the CU board of ``ChartElectricityBalance``: produced heat.

    The CU board carries a single per-day Wh field; that field is what
    the portal shows as "Produzierte Wärme heute".
    """
    cu = (balance.get("boardMap") or {}).get("CU-CHP01") or {}
    out: list[DailyHeat] = []
    for key, values in (cu.get("rows") or {}).items():
        stamped = _row_time(key)
        if stamped is None or not stamped[1]:
            continue
        if not isinstance(values, (list, tuple)) or not values:
            continue
        out.append(
            DailyHeat(
                day=_day_of(stamped[0]),
                produced_kwh=_wh_to_kwh(values[0]),
            )
        )
    out.sort(key=lambda row: row.day)
    return out


def daily_run_rows(operating: Mapping[str, Any]) -> list[DailyRun]:
    """Parse ``ChartOperatingData`` (cumulative hourly rows).

    Fields, positionally: hours in load level 1, 2, 3, total hours,
    starts, produced heat (Wh), producer energy (Wh). The day's figure
    is the last row of that day; rows reset at midnight.
    """
    cu = (operating.get("boardMap") or {}).get("CU-CHP01") or operating
    out: list[DailyRun] = []
    for day, values in _last_row_per_day(cu.get("rows") or {}):
        if len(values) < 5:
            continue
        try:
            starts = int(Decimal(values[4]))
        except Exception:  # noqa: BLE001 — a broken row must not kill the day
            continue
        out.append(
            DailyRun(
                day=day,
                hours=_raw_to_hours(values[3]),
                starts=starts,
                ls2_hours=_raw_to_hours(values[1]),
                ls3_hours=_raw_to_hours(values[2]),
            )
        )
    return out


def shutdown_rows(
    status_chart: Mapping[str, Any],
    *,
    init: Mapping[str, Any] | None,
    language: str,
    translate_enum: Any,
) -> list[ShutdownSample]:
    """Parse ``ChartChpStatus`` hourly ``[state, reason]`` rows.

    ``translate_enum`` is the client's enum resolver
    (``functools.partial(_enum_translation, init, language, ...)``);
    unknown codes stay raw integers so nothing is silently lost.
    """
    em = (status_chart.get("boardMap") or {}).get("EM-CHP01") or status_chart
    out: list[ShutdownSample] = []
    for key, values in (em.get("rows") or {}).items():
        stamped = _row_time(key)
        if stamped is None or stamped[1]:
            continue  # status rows are hourly, not daily
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            continue
        try:
            state_raw = int(Decimal(str(values[0])))
            reason_raw = int(Decimal(str(values[1])))
        except Exception:  # noqa: BLE001
            continue
        state = translate_enum("DeviceState", state_raw)
        reason = translate_enum("ShutDownReason", reason_raw)
        out.append(
            ShutdownSample(
                at=datetime.fromtimestamp(stamped[0], tz=timezone.utc),
                state=state if state is not None else state_raw,
                reason=reason if reason is not None else reason_raw,
            )
        )
    out.sort(key=lambda row: row.at)
    return out
