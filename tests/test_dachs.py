"""Loopback tests for the Dachs device object."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from aiohttp import web

from senertec_dachs import (
    Dachs,
    DachsError,
    KeyType,
    LOCAL_KEYS,
    LocalClient,
    Normalization,
    Reading,
    ReadingState,
)

Handler = Callable[[web.Request], Awaitable[web.Response]]

SYNTHETIC_VALUES = {
    KeyType.INTEGER: "7",
    KeyType.DECIMAL: "1.5",
    KeyType.BOOLEAN: "1",
    KeyType.STRING: "0000000001",
}

#: Wire key -> device property, for every scale-verified scalar key.
DEVICE_PROPERTIES: dict[str, str] = {
    # operating
    "Hka_Bd.Anforderung.ModulAnzahl": "requested_modules",
    "Hka_Bd.Anforderung.UStromF_Anf.bFlagSF": "power_demand_flags",
    "Hka_Bd.UStromF_Frei.bFreigabe": "power_release_flags",
    "Hka_Bd.bStoerung": "fault_code",
    "Hka_Bd.bWarnung": "warning_code",
    "Hka_Bd.UHka_Anf.Anforderung.fStrom": "power_requested",
    "Hka_Bd.UHka_Anf.usAnforderung": "demand_flags",
    "Hka_Bd.UHka_Frei.usFreigabe": "release_flags",
    "Hka_Bd.ulArbeitElektr": "electrical_energy_kwh",
    "Hka_Bd.ulArbeitThermHka": "thermal_energy_kwh",
    "Hka_Bd.ulArbeitThermKon": "condenser_energy_kwh",
    "Hka_Bd.ulBetriebssekunden": "runtime_hours",
    "Hka_Bd.ulAnzahlStarts": "starts",
    # identity
    "Hka_Bd_Stat.uchSeriennummer": "serial_number",
    "Hka_Bd_Stat.uchTeilenummer": "part_number",
    "Hka_Bd_Stat.ulInbetriebnahmedatum": "commissioned_epoch_seconds",
    # year-end counters
    "BD3112.Hka_Bd.ulBetriebssekunden": "year_end_runtime_hours",
    "BD3112.Hka_Bd.ulAnzahlStarts": "year_end_starts",
    "BD3112.Hka_Bd.ulArbeitElektr": "year_end_electrical_energy_kwh",
    "BD3112.Hka_Bd.ulArbeitThermHka": "year_end_thermal_energy_kwh",
    "BD3112.Hka_Bd.ulArbeitThermKon": "year_end_condenser_energy_kwh",
    # second heat generator
    "Brenner_Bd.bIstStatus": "second_generator_status_code",
    "Brenner_Bd.bWarnung": "second_generator_warning_code",
    "Brenner_Bd.UBrenner_Anf.usAnforderung": "second_generator_demand_flags",
    "Brenner_Bd.UBrenner_Frei.bFreigabe": "second_generator_release_flags",
    "Brenner_Bd.ulAnzahlStarts": "second_generator_starts",
    "Brenner_Bd.ulBetriebssekunden": "second_generator_runtime_hours",
    # temperatures
    "Hka_Mw1.Temp.sAbgasHKA": "exhaust_temperature",
    "Hka_Mw1.Temp.sAbgasMotor": "engine_exhaust_temperature",
    "Hka_Mw1.Temp.sKapsel": "enclosure_temperature",
    "Hka_Mw1.Temp.sbAussen": "outdoor_temperature",
    "Hka_Mw1.Temp.sbFreigabeModul": "module_release_temperature",
    "Hka_Mw1.Temp.sbFuehler1": "sensor_1_temperature",
    "Hka_Mw1.Temp.sbFuehler2": "sensor_2_temperature",
    "Hka_Mw1.Temp.sbGen": "generator_inlet_temperature",
    "Hka_Mw1.Temp.sbMotor": "engine_coolant_temperature",
    "Hka_Mw1.Temp.sbRegler": "controller_temperature",
    "Hka_Mw1.Temp.sbRuecklauf": "return_temperature",
    "Hka_Mw1.Temp.sbVorlauf": "supply_temperature",
    "Hka_Mw1.Temp.sbZS_Fuehler3": "sensor_3_temperature",
    "Hka_Mw1.Temp.sbZS_Fuehler4": "sensor_4_temperature",
    "Hka_Mw1.Temp.sbZS_Vorlauf1": "heating_circuit_1_supply_temperature",
    "Hka_Mw1.Temp.sbZS_Vorlauf2": "heating_circuit_2_supply_temperature",
    "Hka_Mw1.Temp.sbZS_Warmwasser": "hot_water_temperature",
    # temperature setpoints
    "Hka_Mw1.Solltemp.sbRuecklauf": "return_temperature_setpoint",
    "Hka_Mw1.Solltemp.sbVorlauf": "supply_temperature_setpoint",
    # actuators
    "Hka_Mw1.Aktor.fFreiAltWaerm": "alternate_heat_enabled",
    "Hka_Mw1.Aktor.fMischer1Auf": "mixer_1_opening",
    "Hka_Mw1.Aktor.fMischer1Zu": "mixer_1_closing",
    "Hka_Mw1.Aktor.fMischer2Auf": "mixer_2_opening",
    "Hka_Mw1.Aktor.fMischer2Zu": "mixer_2_closing",
    "Hka_Mw1.Aktor.fProgAus1": "programmable_output_1",
    "Hka_Mw1.Aktor.fProgAus2": "programmable_output_2",
    "Hka_Mw1.Aktor.fProgAus3": "programmable_output_3",
    "Hka_Mw1.Aktor.fStoerung": "fault_relay",
    "Hka_Mw1.Aktor.fUPHeizkreis1": "heating_circuit_1_pump",
    "Hka_Mw1.Aktor.fUPHeizkreis2": "heating_circuit_2_pump",
    "Hka_Mw1.Aktor.fUPKuehlung": "internal_circulation_pump",
    "Hka_Mw1.Aktor.fUPVordruck": "pre_pressure_pump",
    "Hka_Mw1.Aktor.fUPZirkulation": "circulation_pump",
    "Hka_Mw1.Aktor.fWartung": "maintenance_relay",
    # measurements
    "Hka_Mw1.sWirkleistung": "power_w",
    "Hka_Mw1.ulMotorlaufsekunden": "engine_runtime_hours",
    "Hka_Mw1.usDrehzahl": "engine_speed_rpm",
    # module statistics (scale-verified ones)
    "Mm_MinMax.bModulBhMaxWart": "max_maintenance_module",
    "Mm_MinMax.bModulBhMinWart": "min_maintenance_module",
    "Mm_MinMax.ModulBhMax.bModulNr": "max_runtime_module",
    "Mm_MinMax.ModulBhMin.bModulNr": "min_runtime_module",
    "Mm_MinMax.ModulStartMax.bModulNr": "most_starts_module",
    "Mm_MinMax.ModulStartMax.ulWert": "most_starts",
    "Mm_MinMax.ModulStartMin.bModulNr": "fewest_starts_module",
    "Mm_MinMax.ModulStartMin.ulWert": "fewest_starts",
    # maintenance
    "Wartung_Cache.fStehtAn": "maintenance_due",
    "Wartung_Cache.ulBetriebssekundenBei": "last_maintenance_runtime_hours",
    "Wartung_Cache.ulZeitstempel": "last_maintenance_epoch_seconds",
    "Wartung_Cache.usIntervall": "maintenance_interval_hours",
}


def synthetic_body(requested: list[str], overrides: dict[str, str] | None = None) -> str:
    values = {
        **{key: SYNTHETIC_VALUES[LOCAL_KEYS[key].type] for key in LOCAL_KEYS},
        **(overrides or {}),
    }
    rows = [f"{key}={values[key]}" for key in requested if key in values]
    return "\r\n".join(rows) + "\r\n"


async def start_app(handler: Handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_get("/getKey", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


async def test_update_then_direct_properties() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            snapshot = await dachs.update()
            assert dachs.snapshot is snapshot
            assert dachs.serial_number == "0000000001"
            assert dachs.part_number == "0000000001"
            assert dachs.commissioned_epoch_seconds == 7
            assert dachs.runtime_hours == Decimal("1.5")
            assert dachs.starts == 7
            assert dachs.electrical_energy_kwh == Decimal("1.5")
            assert dachs.thermal_energy_kwh == Decimal("1.5")
            assert dachs.condenser_energy_kwh == Decimal("1.5")
            assert dachs.fault_code == 7
            assert dachs.warning_code == 7
            assert dachs.power_requested is True
            assert dachs.power_w == 1500
            assert dachs.engine_speed_rpm == 7
            assert dachs.engine_runtime_hours == Decimal("1.5")
            assert dachs.outdoor_temperature == Decimal("1.5")
            assert dachs.supply_temperature == Decimal("1.5")
            assert dachs.return_temperature == Decimal("1.5")
            assert dachs.hot_water_temperature == Decimal("1.5")
            assert dachs.running is True
            assert dachs.maintenance_due is True
            assert dachs.maintenance_interval_hours == 7
            assert isinstance(dachs.client, LocalClient)
    finally:
        await runner.cleanup()
    assert dachs.client.closed  # owned client closed by the context manager


async def test_device_covers_every_verified_key() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            await dachs.update()
            # power_w converts the kW reading to whole watts
            converted = {"power_w": lambda value: int(value * 1000)}
            for wire_key, attr in DEVICE_PROPERTIES.items():
                reading = Reading.from_raw(
                    LOCAL_KEYS[wire_key], dachs.snapshot.raw.get(wire_key)
                )
                expected = reading.value if reading.state is ReadingState.AVAILABLE else None
                if expected is not None and attr in converted:
                    expected = converted[attr](expected)
                assert getattr(dachs, attr) == expected, attr
    finally:
        await runner.cleanup()

    # Completeness: scalar properties + structured access + unverified keys
    # together account for the entire documented catalogue.
    structured = {
        f"Mm[{index}].ModulSteuerung.{flag}"
        for index in range(10)
        for flag in ("fModulLaeuft", "fModulVerfuegbar")
    }
    structured |= {
        f"Laufraster15Min_aktTag.bDoppelstunde[{slot}]" for slot in range(12)
    }
    unverified = {
        key
        for key, descriptor in LOCAL_KEYS.items()
        if descriptor.normalization is Normalization.UNVERIFIED
    }
    assert set(DEVICE_PROPERTIES) | structured | unverified == set(LOCAL_KEYS)


async def test_modules_and_daily_run_grid() -> None:
    async def get(request: web.Request) -> web.Response:
        overrides = {
            "Mm[0].ModulSteuerung.fModulLaeuft": "false",
            "Laufraster15Min_aktTag.bDoppelstunde[4]": "255",
        }
        return web.Response(text=synthetic_body(request.query.getall("k"), overrides))

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            await dachs.update()
            assert len(dachs.modules) == 10
            assert dachs.modules[0].running is False
            assert dachs.modules[0].available is True
            assert dachs.modules[1].running is True
            assert dachs.running is False  # shortcut for module 1
            assert len(dachs.daily_run_grid) == 12
            assert dachs.daily_run_grid[4] == 255
            assert dachs.daily_run_grid[0] == 7
    finally:
        await runner.cleanup()


async def test_properties_require_update_first() -> None:
    dachs = Dachs("127.0.0.1", port=1)
    with pytest.raises(RuntimeError):
        _ = dachs.snapshot
    with pytest.raises(RuntimeError):
        _ = dachs.serial_number
    await dachs.close()


async def test_non_available_readings_return_none() -> None:
    async def get(request: web.Request) -> web.Response:
        overrides = {
            "Hka_Mw1.Temp.sbZS_Warmwasser": "127",  # not-connected sentinel
            "Hka_Mw1.Temp.sbAussen": "",  # invalid
        }
        return web.Response(text=synthetic_body(request.query.getall("k"), overrides))

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            await dachs.update()
            assert dachs.hot_water_temperature is None
            assert dachs.outdoor_temperature is None
            # the detail stays reachable through the snapshot
            assert dachs.snapshot.temperatures.hot_water.state is ReadingState.UNAVAILABLE
            assert dachs.snapshot.temperatures.hot_water.raw == "127"
            assert dachs.snapshot.temperatures.outdoor.state is ReadingState.INVALID
    finally:
        await runner.cleanup()


async def test_injected_client_is_not_closed() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app(get)
    try:
        client = LocalClient("127.0.0.1", port=port)
        async with Dachs(client=client) as dachs:
            await dachs.update()
            assert dachs.serial_number == "0000000001"
        assert not client.closed  # stays owned by the caller
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "7"
        }
        await client.close()
    finally:
        await runner.cleanup()


async def test_request_run_via_device_object() -> None:
    posts: list[list[tuple[str, str]]] = []

    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    async def set_keys(request: web.Request) -> web.Response:
        fields = await request.post()
        posts.append([*fields.items()])
        return web.Response(
            text=(
                "Stromf_Ew.Anforderung_GLT.bAktiv ok\n"
                "Stromf_Ew.Anforderung_GLT.bAnzahlModule ok\n"
            )
        )

    app = web.Application()
    app.router.add_get("/getKey", get)
    app.router.add_post("/setKeys", set_keys)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            await dachs.update()
            await dachs.request_run()
    finally:
        await runner.cleanup()
    assert posts == [
        [
            ("Stromf_Ew.Anforderung_GLT.bAktiv", "1"),
            ("Stromf_Ew.Anforderung_GLT.bAnzahlModule", "1"),
        ]
    ]


async def test_failed_local_update_marks_device_offline() -> None:
    """A failed local update sets device_online=False; the error still raises."""
    async def get(request: web.Request) -> web.Response:
        return web.Response(status=503, text="busy")

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            with pytest.raises(DachsError):
                await dachs.update()
            assert dachs.device_online is False
            assert dachs.data_fetched_at is None
            with pytest.raises(RuntimeError):
                _ = dachs.snapshot  # failed update stored no snapshot
    finally:
        await runner.cleanup()

async def test_failed_update_keeps_previous_snapshot_and_flips_offline() -> None:
    """A later failure preserves the old snapshot but reports offline."""
    fail = False

    async def get(request: web.Request) -> web.Response:
        if fail:
            raise web.HTTPServiceUnavailable
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app(get)
    try:
        async with Dachs("127.0.0.1", port=port) as dachs:
            await dachs.update()
            first = dachs.snapshot
            assert dachs.device_online is True
            fail = True
            with pytest.raises(DachsError):
                await dachs.update()
            assert dachs.device_online is False
            assert dachs.snapshot is first  # previous data preserved
    finally:
        await runner.cleanup()

def test_constructor_arguments_are_exclusive() -> None:
    client = LocalClient("127.0.0.1")
    with pytest.raises(ValueError):
        Dachs("127.0.0.1", client=client)
    with pytest.raises(ValueError):
        Dachs()
