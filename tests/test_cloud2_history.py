"""Typed history aggregates: daily energy, run, shutdowns, unit details."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from test_cloud2_client import (
    EMAIL,
    FakeDachsConnect,
    PASSWORD,
    SERIAL,
    _init_payload,
    make_client,
)

from senertec_dachs.cloud2.history import (
    DailyElectric,
    DailyHeat,
    DailyRun,
    ShutdownSample,
)

# 2026-11-10 11:00 UTC and later hours (matches the probed demo data).
T_NOV10_11 = 1794308400
T_NOV10_17 = 1794333600
T_NOV11_09 = 1794390800
DAY_LABEL = '{"time": 1794308400, "format": "d"}'


def _producing_chart(rows: dict) -> dict:
    return {
        "from": T_NOV10_11 * 1000,
        "to": T_NOV11_09 * 1000,
        "boardMap": {
            "common": {
                "boardName": "common",
                "fields": 3,
                "totalRows": len(rows),
                "fieldConfig": {
                    "SelfConsumption": {"obd": {"gain": "0.001", "unit": "kWh"}},
                    "TotalConsumption": {"obd": {"gain": "0.001", "unit": "kWh"}},
                },
                "rows": rows,
            }
        },
        "charts": {},
        "config": {},
    }


def _balance_chart(em_rows: dict, cu_rows: dict) -> dict:
    return {
        "from": T_NOV10_11 * 1000,
        "to": T_NOV11_09 * 1000,
        "boardMap": {
            "EM-CHP01": {
                "boardName": "EM-CHP01",
                "fields": 2,
                "totalRows": len(em_rows),
                "fieldConfig": {},
                "rows": em_rows,
            },
            "CU-CHP01": {
                "boardName": "CU-CHP01",
                "fields": 1,
                "totalRows": len(cu_rows),
                "fieldConfig": {},
                "rows": cu_rows,
            },
        },
        "charts": {},
        "config": {},
    }


def _operating_chart(rows: dict) -> dict:
    return {
        "from": T_NOV10_11 * 1000,
        "to": T_NOV11_09 * 1000,
        "boardMap": {
            "CU-CHP01": {
                "boardName": "CU-CHP01",
                "fields": 7,
                "totalRows": len(rows),
                "fieldConfig": {},
                "rows": rows,
            }
        },
        "charts": {},
        "config": {},
    }


def _status_chart(rows: dict) -> dict:
    return {
        "from": T_NOV10_11 * 1000,
        "to": T_NOV11_09 * 1000,
        "boardMap": {
            "EM-CHP01": {
                "boardName": "EM-CHP01",
                "fields": 2,
                "totalRows": len(rows),
                "fieldConfig": {},
                "rows": rows,
            }
        },
        "charts": {},
        "config": {},
    }


class HistoryFake(FakeDachsConnect):
    """FakeDachsConnect with canned history charts per name."""

    def __init__(self) -> None:
        super().__init__()
        self.canned_charts: dict[str, dict] = {}

    async def rest_init(self, request) -> __import__("aiohttp").web.Response:
        from aiohttp import web

        body = _init_payload()
        en = body["translations"].setdefault("en-gb", {})
        en["enums"] = [
            {"name": "OffOn", "translations": {"0": "Off", "1": "On"}},
            {
                "name": "ShutDownReason",
                "translations": {
                    "0": "Undefined",
                    "1": "Manual",
                    "2": "No demand",
                    "3": "Error",
                },
            },
            {
                "name": "DeviceState",
                "translations": {
                    "0": "Standby",
                    "4": "Generator DHW",
                },
            },
        ]
        return web.json_response(body)

    async def rest_chart(self, request) -> __import__("aiohttp").web.Response:
        from aiohttp import web

        payload = await request.json()
        name = payload.get("chartName")
        body = self.canned_charts.get(name)
        if body is None:
            return web.json_response({"message": "unknown chart"}, status=400)
        return web.json_response(body)


@pytest.fixture
async def history_service() -> HistoryFake:
    fake = HistoryFake()
    fake.app.middlewares.append(fake.idp_middleware)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


async def test_daily_electric_parses_days(history_service: HistoryFake) -> None:
    history_service.canned_charts["ChartEnergyProducing"] = _producing_chart(
        {DAY_LABEL: ["3566.0", 5833, 92]}
    )
    history_service.canned_charts["ChartElectricityBalance"] = _balance_chart(
        em_rows={DAY_LABEL: [3374, 92]}, cu_rows={DAY_LABEL: [9491]}
    )
    async with make_client(history_service, serial=SERIAL) as client:
        await client.connect()
        days = await client.daily_electric()
    assert days == [
        DailyElectric(
            day=datetime.fromtimestamp(T_NOV10_11, tz=timezone.utc).date(),
            self_consumed_kwh=Decimal("3.566"),
            total_consumed_kwh=Decimal("5.833"),
            fed_in_kwh=Decimal("0.092"),
            from_grid_kwh=Decimal("3.374"),
            to_grid_kwh=Decimal("0.092"),
        )
    ]


async def test_daily_heat_from_balance(history_service: HistoryFake) -> None:
    history_service.canned_charts["ChartElectricityBalance"] = _balance_chart(
        em_rows={DAY_LABEL: [3374, 92]}, cu_rows={DAY_LABEL: [9491]}
    )
    async with make_client(history_service, serial=SERIAL) as client:
        await client.connect()
        days = await client.daily_heat()
    assert days == [
        DailyHeat(
            day=datetime.fromtimestamp(T_NOV10_11, tz=timezone.utc).date(),
            produced_kwh=Decimal("9.491"),
        )
    ]


async def test_daily_run_takes_last_row_per_day(history_service: HistoryFake) -> None:
    # Two cumulative rows on day 1 (11:00 → 17:00) and one on day 2.
    history_service.canned_charts["ChartOperatingData"] = _operating_chart(
        {
            str(T_NOV10_11): [11391, 0, 0, 11391, 1, 9491, 22900],
            str(T_NOV10_17): [7200, 3600, 0, 7200, 1, 0, 0],
            str(T_NOV11_09): [14486, 0, 0, 14486, 2, 12057, 29529],
        }
    )
    async with make_client(history_service, serial=SERIAL) as client:
        await client.connect()
        days = await client.daily_run()
    day1 = datetime.fromtimestamp(T_NOV10_11, tz=timezone.utc).date()
    day2 = datetime.fromtimestamp(T_NOV11_09, tz=timezone.utc).date()
    assert days == [
        DailyRun(
            day=day1,
            hours=Decimal("7200") / Decimal("3600"),
            starts=1,
            ls2_hours=Decimal("1"),
            ls3_hours=Decimal("0"),
        ),
        DailyRun(
            day=day2,
            hours=Decimal("14486") / Decimal("3600"),
            starts=2,
            ls2_hours=Decimal("0"),
            ls3_hours=Decimal("0"),
        ),
    ]


async def test_shutdown_history_translates_reasons(
    history_service: HistoryFake,
) -> None:
    history_service.canned_charts["ChartChpStatus"] = _status_chart(
        {str(T_NOV10_11): ["4.0", "2.0"], str(T_NOV10_17): ["0.0", "2.0"]}
    )
    async with make_client(history_service, serial=SERIAL) as client:
        await client.connect()
        samples = await client.shutdown_history()
    assert len(samples) == 2
    first = samples[0]
    assert isinstance(first, ShutdownSample)
    assert first.at == datetime.fromtimestamp(T_NOV10_11, tz=timezone.utc)
    # ShutDownReason 2 = "No demand" (en-gb) — the portal's "Keine Anforderung".
    assert first.reason == "No demand"
    assert first.state is not None  # translated or raw, never lost


async def test_unit_details_from_payload(history_service: HistoryFake) -> None:
    async with make_client(history_service, serial=SERIAL) as client:
        await client.connect()
        details = client.unit_details()
    assert details.serial == SERIAL
    assert details.model == "Dachs Gen2 F5.5"
    assert details.product_group == "dachsII"
    assert details.article_number == "8000.013.000"
    assert details.commissioning_date == datetime(
        2018, 12, 22, 23, 0, tzinfo=timezone.utc
    )
    assert details.location_name == "Demo"
    assert details.has_service_contract is True
    assert details.warnings == ()
