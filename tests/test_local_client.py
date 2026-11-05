"""Loopback tests for LocalClient against in-process aiohttp servers."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from senertec_dachs import (
    AuthenticationError,
    ConnectionError,
    HttpError,
    KeyType,
    LOCAL_KEYS,
    LocalClient,
    ProtocolError,
    ReadingState,
    RequestTimeoutError,
    WriteError,
)

PROTOCOL_SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "local-protocol.json").read_text(
        encoding="utf-8"
    )
)
READ_ERROR_SAMPLES = [s for s in PROTOCOL_SAMPLES["read"] if "error" in s]
WRITE_ERROR_SAMPLES = [s for s in PROTOCOL_SAMPLES["write"] if "error" in s]

AUTH_HEADER = "Basic " + base64.b64encode(b"glt:").decode("ascii")

Handler = Callable[[web.Request], Awaitable[web.Response]]

SYNTHETIC_VALUES = {
    KeyType.INTEGER: "7",
    KeyType.DECIMAL: "1.5",
    KeyType.BOOLEAN: "1",
    KeyType.STRING: "0000000001",
}


def synthetic_body(requested: list[str]) -> str:
    rows = [
        f"{key}={SYNTHETIC_VALUES[LOCAL_KEYS[key].type]}"
        for key in requested
        if key in LOCAL_KEYS
    ]
    return "\r\n".join(rows) + "\r\n"


async def start_app(routes: list[tuple[str, str, Handler]]) -> tuple[web.AppRunner, int]:
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


async def _drop_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    writer.close()
    await writer.wait_closed()


# --- reads ---------------------------------------------------------------


async def test_read_keys_reversed_order_and_authentication() -> None:
    values = {
        "Hka_Bd_Stat.uchTeilenummer": "4700046004",
        "Hka_Bd_Stat.uchSeriennummer": "0000000001",
        "Hka_Bd.ulBetriebssekunden": "35289",
    }
    calls: list[list[str]] = []

    async def get(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == AUTH_HEADER
        requested = request.query.getall("k")
        calls.append(requested)
        rows = [f"{key}={values[key]}" for key in reversed(requested)]
        return web.Response(text="\r\n".join(rows) + "\r\n")

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            assert await client.read_keys(list(values)) == values
    finally:
        await runner.cleanup()
    assert calls == [list(values)]


async def test_read_keys_empty_request_performs_no_io() -> None:
    async with LocalClient("127.0.0.1", port=1) as client:
        assert await client.read_keys([]) == {}


async def test_read_keys_deduplicates_preserving_order() -> None:
    calls: list[list[str]] = []

    async def get(request: web.Request) -> web.Response:
        requested = request.query.getall("k")
        calls.append(requested)
        return web.Response(text=synthetic_body(requested))

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            result = await client.read_keys(
                ["Hka_Bd.ulAnzahlStarts", "Hka_Bd.ulAnzahlStarts", "Hka_Mw1.usDrehzahl"]
            )
    finally:
        await runner.cleanup()
    assert calls == [["Hka_Bd.ulAnzahlStarts", "Hka_Mw1.usDrehzahl"]]
    assert result == {"Hka_Bd.ulAnzahlStarts": "7", "Hka_Mw1.usDrehzahl": "7"}


async def test_read_keys_allows_undocumented_keys() -> None:
    async def get(request: web.Request) -> web.Response:
        rows = [f"{key}=5" for key in request.query.getall("k")]
        return web.Response(text="\n".join(rows))

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            assert await client.read_keys(["Undocumented.Key"]) == {
                "Undocumented.Key": "5"
            }
    finally:
        await runner.cleanup()


async def test_read_keys_rejects_invalid_keys_before_io() -> None:
    async with LocalClient("127.0.0.1", port=1) as client:
        with pytest.raises(ValueError):
            await client.read_keys([""])
        with pytest.raises(ValueError):
            await client.read_keys(["has space"])


@pytest.mark.parametrize("sample", READ_ERROR_SAMPLES, ids=lambda s: s["name"])
async def test_read_keys_raises_protocol_error(sample: dict) -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=sample["response_text"])

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(ProtocolError):
                await client.read_keys(sample["requested"])
    finally:
        await runner.cleanup()


async def test_read_readings_returns_typed_values() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app([("GET", "/getKey", get)])
    keys = [
        "Hka_Bd.ulArbeitElektr",
        "Mm[0].ModulSteuerung.fModulLaeuft",
        "Hka_Mw1.Temp.sbAussen",
        "Hka_Ew.HydraulikNr.bSpeicherArt",
    ]
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            readings = await client.read_readings(keys)
    finally:
        await runner.cleanup()
    assert list(readings) == keys
    assert readings["Hka_Bd.ulArbeitElektr"].value == Decimal("1.5")
    assert readings["Mm[0].ModulSteuerung.fModulLaeuft"].value is True
    assert readings["Hka_Mw1.Temp.sbAussen"].value == Decimal("1.5")
    assert readings["Hka_Ew.HydraulikNr.bSpeicherArt"].state is ReadingState.UNVERIFIED
    assert readings["Hka_Ew.HydraulikNr.bSpeicherArt"].value is None


async def test_read_readings_rejects_unknown_keys_before_io() -> None:
    async with LocalClient("127.0.0.1", port=1) as client:
        with pytest.raises(ValueError, match="read_keys"):
            await client.read_readings(["Undocumented.Key"])


async def test_read_snapshot_covers_all_documented_keys() -> None:
    calls: list[list[str]] = []

    async def get(request: web.Request) -> web.Response:
        requested = request.query.getall("k")
        calls.append(requested)
        return web.Response(text=synthetic_body(requested))

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            snapshot = await client.read_snapshot()
    finally:
        await runner.cleanup()
    assert len(snapshot.raw) == 118
    assert sum(len(chunk) for chunk in calls) == 118
    assert snapshot.operating.electrical_energy.value == Decimal("1.5")
    assert snapshot.identity.serial_number.value == "0000000001"
    assert snapshot.modules[0].running.value is True
    assert snapshot.temperatures.outdoor.state is ReadingState.AVAILABLE


async def test_read_keys_splits_requests_within_url_limit() -> None:
    calls: list[list[str]] = []

    async def get(request: web.Request) -> web.Response:
        requested = request.query.getall("k")
        calls.append(requested)
        return web.Response(text=synthetic_body(requested))

    runner, port = await start_app([("GET", "/getKey", get)])
    keys = [f"Laufraster15Min_aktTag.bDoppelstunde[{i}]" for i in range(12)]
    try:
        async with LocalClient("127.0.0.1", port=port, max_url_length=140) as client:
            result = await client.read_keys(keys)
    finally:
        await runner.cleanup()
    assert result == {key: "7" for key in keys}
    assert len(calls) > 1
    assert [key for chunk in calls for key in chunk] == keys
    base = URL.build(scheme="http", host="127.0.0.1", port=port, path="/getKey")
    for chunk in calls:
        url = base.with_query([("k", key) for key in chunk])
        assert len(str(url)) <= 140


async def test_read_keys_single_oversized_key_fails_before_io() -> None:
    async with LocalClient("127.0.0.1", port=1, max_url_length=40) as client:
        with pytest.raises(ValueError):
            await client.read_keys(["Hka_Mw1.Temp.sbZS_Vorlauf2"])


# --- transport errors ----------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_rejected(status: int) -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(status=status)

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(AuthenticationError):
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        await runner.cleanup()


async def test_http_error_status_preserved() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(status=500)

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(HttpError) as excinfo:
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        await runner.cleanup()
    assert excinfo.value.status == 500
    assert excinfo.value.outcome_unknown is False


async def test_redirect_is_not_followed() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": "/elsewhere"})

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(HttpError) as excinfo:
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        await runner.cleanup()
    assert excinfo.value.status == 302


async def test_request_timeout() -> None:
    async def get(request: web.Request) -> web.Response:
        await asyncio.sleep(1.0)
        return web.Response(text="Hka_Bd.ulAnzahlStarts=1\n")

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        async with LocalClient("127.0.0.1", port=port, timeout=0.1) as client:
            with pytest.raises(RequestTimeoutError):
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        await runner.cleanup()


async def test_connection_failure() -> None:
    server = await asyncio.start_server(_drop_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with LocalClient("127.0.0.1", port=port, timeout=5) as client:
            with pytest.raises(ConnectionError):
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        server.close()
        await server.wait_closed()


# --- lifecycle -----------------------------------------------------------


async def test_session_ownership() -> None:
    async def get(request: web.Request) -> web.Response:
        return web.Response(text=synthetic_body(request.query.getall("k")))

    runner, port = await start_app([("GET", "/getKey", get)])
    try:
        caller_session = aiohttp.ClientSession()
        client = LocalClient("127.0.0.1", port=port, session=caller_session)
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "7"
        }
        await client.close()
        assert not caller_session.closed
        async with caller_session.get(
            f"http://127.0.0.1:{port}/getKey", params=[("k", "Hka_Bd.ulAnzahlStarts")]
        ) as response:
            assert response.status == 200
        await caller_session.close()

        owned = LocalClient("127.0.0.1", port=port)
        assert await owned.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "7"
        }
        owned_session = owned._session
        await owned.close()
        assert owned_session is not None
        assert owned_session.closed
    finally:
        await runner.cleanup()


async def test_closed_client_rejects_operations_and_close_is_idempotent() -> None:
    client = LocalClient("127.0.0.1", port=1)
    await client.close()
    await client.close()
    assert client.closed
    with pytest.raises(RuntimeError):
        await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    with pytest.raises(RuntimeError):
        await client.request_power(1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": ""},
        {"host": "  "},
        {"host": "http://192.168.1.29"},
        {"host": "192.168.1.29/getKey"},
        {"host": "dachs?k=1"},
        {"host": "da chs"},
        {"host": "dachs#fragment"},
        {"host": "user@dachs"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": True},
        {"max_url_length": 0},
        {"max_url_length": -5},
    ],
)
def test_invalid_configuration_raises_value_error(kwargs: dict) -> None:
    parameters: dict = {
        "host": "dachs",
        "port": 8080,
        "timeout": 10.0,
        "max_url_length": 1800,
    }
    parameters.update(kwargs)
    with pytest.raises(ValueError):
        LocalClient(**parameters)


def test_ipv6_hosts_are_bracketed() -> None:
    assert str(LocalClient("::1")._get_url) == "http://[::1]:8080/getKey"
    assert str(LocalClient("[fe80::1]")._set_url) == "http://[fe80::1]:8080/setKeys"


# --- power demand --------------------------------------------------------


async def test_request_power_sends_documented_fields_once() -> None:
    posts: list[list[tuple[str, str]]] = []

    async def set_keys(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == AUTH_HEADER
        assert request.content_type == "application/x-www-form-urlencoded"
        fields = await request.post()
        posts.append([*fields.items()])
        return web.Response(
            text=(
                "Stromf_Ew.Anforderung_GLT.bAnzahlModule ok\n"
                "Stromf_Ew.Anforderung_GLT.bAktiv ok\n"
            )
        )

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            await client.request_power(2)
    finally:
        await runner.cleanup()
    assert posts == [
        [
            ("Stromf_Ew.Anforderung_GLT.bAktiv", "1"),
            ("Stromf_Ew.Anforderung_GLT.bAnzahlModule", "2"),
        ]
    ]


async def test_request_run_uses_single_module_demand() -> None:
    posts: list[dict[str, str]] = []

    async def set_keys(request: web.Request) -> web.Response:
        fields = await request.post()
        posts.append(dict(fields))
        return web.Response(
            text=(
                "Stromf_Ew.Anforderung_GLT.bAktiv ok\n"
                "Stromf_Ew.Anforderung_GLT.bAnzahlModule ok\n"
            )
        )

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            await client.request_run()
    finally:
        await runner.cleanup()
    assert posts == [
        {
            "Stromf_Ew.Anforderung_GLT.bAktiv": "1",
            "Stromf_Ew.Anforderung_GLT.bAnzahlModule": "1",
        }
    ]

@pytest.mark.parametrize("modules", [0, 10])
async def test_request_power_accepts_boundary_counts(modules: int) -> None:
    posts: list[dict[str, str]] = []

    async def set_keys(request: web.Request) -> web.Response:
        fields = await request.post()
        posts.append(dict(fields))
        return web.Response(
            text=(
                "Stromf_Ew.Anforderung_GLT.bAktiv ok\n"
                "Stromf_Ew.Anforderung_GLT.bAnzahlModule ok\n"
            )
        )

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            await client.request_power(modules)
    finally:
        await runner.cleanup()
    assert posts == [
        {
            "Stromf_Ew.Anforderung_GLT.bAktiv": "1",
            "Stromf_Ew.Anforderung_GLT.bAnzahlModule": str(modules),
        }
    ]


@pytest.mark.parametrize("modules", [-1, 11, True, "1", 1.5, None])
async def test_request_power_rejects_invalid_counts_before_io(
    modules: object,
) -> None:
    posts: list[object] = []

    async def set_keys(request: web.Request) -> web.Response:
        await request.post()
        posts.append(1)
        return web.Response(
            text=(
                "Stromf_Ew.Anforderung_GLT.bAktiv ok\n"
                "Stromf_Ew.Anforderung_GLT.bAnzahlModule ok\n"
            )
        )

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(ValueError):
                await client.request_power(modules)  # type: ignore[arg-type]
    finally:
        await runner.cleanup()
    assert posts == []


@pytest.mark.parametrize("sample", WRITE_ERROR_SAMPLES, ids=lambda s: s["name"])
async def test_request_power_acknowledgement_failures_are_uncertain(
    sample: dict,
) -> None:
    posts: list[object] = []

    async def set_keys(request: web.Request) -> web.Response:
        await request.post()
        posts.append(1)
        return web.Response(text=sample["response_text"])

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(WriteError) as excinfo:
                await client.request_power(1)
    finally:
        await runner.cleanup()
    assert excinfo.value.outcome_unknown is True
    assert excinfo.value.acknowledgements is not None
    assert len(posts) == 1


async def test_request_power_http_failure_marks_outcome_unknown() -> None:
    posts: list[object] = []

    async def set_keys(request: web.Request) -> web.Response:
        await request.post()
        posts.append(1)
        return web.Response(status=500)

    runner, port = await start_app([("POST", "/setKeys", set_keys)])
    try:
        async with LocalClient("127.0.0.1", port=port) as client:
            with pytest.raises(HttpError) as excinfo:
                await client.request_power(1)
    finally:
        await runner.cleanup()
    assert excinfo.value.status == 500
    assert excinfo.value.outcome_unknown is True
    assert len(posts) == 1
