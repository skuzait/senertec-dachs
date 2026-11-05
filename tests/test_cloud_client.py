"""Loopback tests for the Dachsportal (cloud) client against a fake portal.

The fake reproduces the observed portal behaviour: token + cookie
sessions, the 401 ``{"relogin": true}`` expiry shape, failed logins
invalidating the current session, ``app.*`` blocks on every ``get``,
and unformatted JSON values.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from aiohttp import web

from senertec_dachs import (
    AuthenticationError,
    CloudClient,
    CloudStatus,
    ConnectionError,
    Dachs,
    HttpError,
    RequestTimeoutError,
    Snapshot,
    WriteError,
)

PORTAL_ID = "12345"
PASSWORD = "secret"

PORTAL_VALUES: dict[str, object] = {
    "Hka_Bd.ulAnzahlStarts@1": 24989,
    "Hka_Bd.ulBetriebssekunden@1": 77888.39,
    "Hka_Bd.ulArbeitElektr@1": 429853.864,
    "Hka_Bd_Stat.uchSeriennummer@1": "2111123450",
    "Hka_Bd_Stat.ulInbetriebnahmedatum@1": 362800800.0,
    "Mm[0].ModulSteuerung.fModulLaeuft@1": True,
    "Mm[1].ModulSteuerung.fModulLaeuft@1": False,
    "Hka_Mw1.Temp.sbZS_Vorlauf2@1": 127,
    "Hka_Ew.HydraulikNr.bSpeicherArt@1": 1,
    "Nonexistent.Key@1": "",
}
APP_BLOCK = {
    "app.dachs_status": 1,
    "app.dachs_automatik": 1,
    "app.module.L1.verfuegbar": True,
}
METAINFO = {
    "offline": False,
    "live": True,
    "module": ["L1"],
    "fetchedAt": 1793879587907,
    "type": "ethernet",
}


class FakePortal:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.fail_login = False
        self.always_401 = False
        self.get_status: int | None = None
        self.get_delay = 0.0
        self.set_reject = False
        self.set_status: int | None = None
        self.offline: bool | None = None
        self.offline_fetched_at: int | None = None
        self.app = web.Application()
        self.app.router.add_post("/checkLogin", self.check_login)
        self.app.router.add_post("/get", self.get)
        self.app.router.add_post("/set", self.set_keys)
        self.app.router.add_post("/metainfoLong", self.metainfo)
        self.app.router.add_post("/nachrichten", self.nachrichten)
        self.app.router.add_post("/reporting", self.reporting)
        self.app.router.add_post("/tageslauf", self.tageslauf)
        self.app.router.add_post("/schaltzeiten", self.schaltzeiten)
        self.app.router.add_post("/dachsAnfordern", self.dachs_anfordern)
        self.app.router.add_post("/statistik", self.statistik)

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    async def stop(self) -> None:
        await self.runner.cleanup()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def count(self, endpoint: str) -> int:
        return sum(1 for name, _ in self.requests if name == endpoint)

    async def check_login(self, request: web.Request) -> web.Response:
        fields = dict(await request.post())
        self.requests.append(("checkLogin", fields))
        ok = (
            not self.fail_login
            and fields.get("sn") == PORTAL_ID
            and fields.get("passwd") == PASSWORD
            and fields.get("revision") == "v2.0.0-265"
            and fields.get("apiRevision") == "666"
        )
        if not ok:
            # A failed login invalidates the session server-side.
            self.valid_tokens.clear()
            return web.json_response(
                {"message": "Logindaten inkorrekt!", "loginOk": False}
            )
        self.tokens_issued += 1
        token = f"token{self.tokens_issued}"
        self.valid_tokens.add(token)
        response = web.json_response(
            {
                "message": "Logindaten erfolgreich übernommen!",
                "loginOk": True,
                "token": token,
                "datenschutz": True,
            }
        )
        response.set_cookie("JSESSIONID", f"session{self.tokens_issued}")
        return response

    def _token_ok(self, fields: dict) -> bool:
        return not self.always_401 and fields.get("token") in self.valid_tokens

    async def get(self, request: web.Request) -> web.Response:
        fields = dict(await request.post())
        self.requests.append(("get", fields))
        assert request.cookies.get("JSESSIONID"), "session cookie must be sent"
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        if self.get_status is not None:
            return web.json_response({"message": "boom"}, status=self.get_status)
        if not self._token_ok(fields):
            return web.json_response(
                {"success": False, "relogin": True}, status=401
            )
        out = dict(APP_BLOCK)
        for entry in json.loads(fields["keys"]):
            assert entry["format"] == ""
            if entry["key"] in PORTAL_VALUES:
                out[entry["key"]] = PORTAL_VALUES[entry["key"]]
        return web.json_response(out)

    async def set_keys(self, request: web.Request) -> web.Response:
        fields = dict(await request.post())
        self.requests.append(("set", fields))
        if not self._token_ok(fields):
            return web.json_response(
                {"success": False, "relogin": True}, status=401
            )
        if self.set_status is not None:
            return web.json_response({"message": "boom"}, status=self.set_status)
        if self.set_reject:
            return web.json_response({"ok": False, "message": "Nicht erlaubt"})
        return web.json_response(
            {"ok": True, "message": "Die Änderungen wurden übernommen."}
        )

    async def _guarded(self, request: web.Request, name: str) -> web.Response | None:
        fields = dict(await request.post())
        self.requests.append((name, fields))
        if not self._token_ok(fields):
            return web.json_response(
                {"success": False, "relogin": True}, status=401
            )
        return None

    async def metainfo(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "metainfoLong")
        if guarded is not None:
            return guarded
        payload = dict(METAINFO)
        if self.offline is not None:
            payload["offline"] = self.offline
        if self.offline_fetched_at is not None:
            payload["fetchedAt"] = self.offline_fetched_at
        return web.json_response(payload)

    async def nachrichten(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "nachrichten")
        if guarded is not None:
            return guarded
        return web.json_response([{"id": 1, "titel": "News", "kurztext": "Hallo"}])

    async def reporting(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "reporting")
        if guarded is not None:
            return guarded
        return web.json_response(
            [{"datum": "23.01.2025", "message": "Wartung durchgeführt", "summary": "Fax: 1", "callcause": 4}]
        )

    async def tageslauf(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "tageslauf")
        if guarded is not None:
            return guarded
        return web.json_response(
            {"success": True, "values": ["dachs"], "data": {"2025-06-15": {"soll": [1], "dachs": [0]}}}
        )

    async def schaltzeiten(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "schaltzeiten")
        if guarded is not None:
            return guarded
        return web.json_response(
            {"mo": [[240, 600]], "di": [], "mi": [], "do": [], "fr": [], "sa": [], "so": [], "_uhr_": False}
        )

    async def dachs_anfordern(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "dachsAnfordern")
        if guarded is not None:
            return guarded
        if self.set_reject:
            return web.json_response({"ok": False, "message": "Nicht erlaubt"})
        return web.json_response({"ok": True, "message": "Laufanforderung abgesetzt!"})

    async def statistik(self, request: web.Request) -> web.Response:
        guarded = await self._guarded(request, "statistik")
        if guarded is not None:
            return guarded
        return web.json_response({"ok": True, "data": [{"bstd": 77580}]})


@pytest.fixture
async def portal() -> FakePortal:
    fake = FakePortal()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


# --- login and value conversion -------------------------------------------


async def test_login_and_value_conversion(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        values = await client.read_keys(
            [
                "Hka_Bd.ulAnzahlStarts",
                "Hka_Bd.ulBetriebssekunden",
                "Hka_Bd.ulArbeitElektr",
                "Hka_Bd_Stat.uchSeriennummer",
                "Hka_Bd_Stat.ulInbetriebnahmedatum",
                "Mm[0].ModulSteuerung.fModulLaeuft",
                "Hka_Mw1.Temp.sbZS_Vorlauf2",
                "Nonexistent.Key",
            ]
        )
    assert values == {
        "Hka_Bd.ulAnzahlStarts": "24989",
        "Hka_Bd.ulBetriebssekunden": "77888.39",
        "Hka_Bd.ulArbeitElektr": "429853.864",
        "Hka_Bd_Stat.uchSeriennummer": "2111123450",
        "Hka_Bd_Stat.ulInbetriebnahmedatum": "362800800.0",
        "Mm[0].ModulSteuerung.fModulLaeuft": "true",
        "Hka_Mw1.Temp.sbZS_Vorlauf2": "127",
        "Nonexistent.Key": "",
    }
    logins = [fields for name, fields in portal.requests if name == "checkLogin"]
    assert len(logins) == 1
    assert logins[0]["sn"] == PORTAL_ID
    assert logins[0]["passwd"] == PASSWORD
    assert logins[0]["revision"] == "v2.0.0-265"
    assert logins[0]["apiRevision"] == "666"


async def test_readings_stay_exact_decimals(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        readings = await client.read_readings(
            ["Hka_Bd.ulArbeitElektr", "Mm[1].ModulSteuerung.fModulLaeuft", "Hka_Ew.HydraulikNr.bSpeicherArt"]
        )
    assert readings["Hka_Bd.ulArbeitElektr"].value == Decimal("429853.864")
    assert readings["Mm[1].ModulSteuerung.fModulLaeuft"].value is False
    assert readings["Hka_Ew.HydraulikNr.bSpeicherArt"].value is None  # unverified scale
    assert readings["Hka_Ew.HydraulikNr.bSpeicherArt"].raw == "1"


async def test_device_object_over_cloud(portal: FakePortal) -> None:
    async with Dachs.cloud(PORTAL_ID, PASSWORD, base_url=portal.url()) as dachs:
        snapshot = await dachs.update()
        assert isinstance(snapshot, Snapshot)
        assert dachs.serial_number == "2111123450"
        assert dachs.runtime_hours == Decimal("77888.39")
        assert dachs.starts == 24989
        assert dachs.electrical_energy_kwh == Decimal("429853.864")
        assert dachs.running is True
        assert dachs.heating_circuit_2_supply_temperature is None  # 127 sentinel
        assert dachs.device_online is True
        expected_fetched = (
            datetime.fromtimestamp(1793879587, tz=timezone.utc).replace(microsecond=907000)
        )
        assert dachs.data_fetched_at == expected_fetched
        status = dachs.client.last_status
        assert isinstance(status, CloudStatus)
        assert status.device_online is True
        assert status.modules == ("L1",)
    assert dachs.client.closed


async def test_offline_device_is_flagged_not_hidden(portal: FakePortal) -> None:
    portal.offline = True
    portal.offline_fetched_at = 1793000000000  # ~10 days old
    async with Dachs.cloud(PORTAL_ID, PASSWORD, base_url=portal.url()) as dachs:
        await dachs.update()
        assert dachs.device_online is False  # portal cannot reach the Dachs
        assert dachs.data_fetched_at == datetime.fromtimestamp(
            1793000000, tz=timezone.utc
        )
        # the (stale) values still flow
        assert dachs.starts == 24989


# --- session handling -------------------------------------------------------


async def test_friendly_authentication_error(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, "wrong-password", base_url=portal.url()) as client:
        with pytest.raises(AuthenticationError) as excinfo:
            await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    message = str(excinfo.value)
    assert "portal ID" in message
    assert "Logindaten inkorrekt!" in message
    assert "12345" in message


async def test_relogin_on_expired_session(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "24989"
        }
        portal.valid_tokens.clear()  # token expired server-side
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "24989"
        }
    assert portal.count("checkLogin") == 2
    # Three gets: the initial read, the one rejected by token expiry,
    # and the retry after the transparent re-login.
    assert portal.count("get") == 3


async def test_relogin_does_not_help_is_friendly(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        portal.always_401 = True
        with pytest.raises(AuthenticationError) as excinfo:
            await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    assert "session expired" in str(excinfo.value)


async def test_failed_login_kills_session_then_recovers(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "24989"
        }
        # A failed login attempt (e.g. from another tool) invalidates the session.
        portal.fail_login = True
        async with CloudClient(PORTAL_ID, "wrong", base_url=portal.url()) as other:
            with pytest.raises(AuthenticationError):
                await other.read_keys(["Hka_Bd.ulAnzahlStarts"])
        portal.fail_login = False
        # The original client recovers with one transparent re-login.
        assert await client.read_keys(["Hka_Bd.ulAnzahlStarts"]) == {
            "Hka_Bd.ulAnzahlStarts": "24989"
        }


# --- transport errors ---------------------------------------------------------


async def test_http_error_is_friendly(portal: FakePortal) -> None:
    portal.get_status = 500
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        with pytest.raises(HttpError) as excinfo:
            await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    assert excinfo.value.status == 500
    assert "Dachsportal" in str(excinfo.value)


async def test_timeout_is_friendly(portal: FakePortal) -> None:
    portal.get_delay = 1.0
    async with CloudClient(
        PORTAL_ID, PASSWORD, base_url=portal.url(), timeout=0.1
    ) as client:
        with pytest.raises(RequestTimeoutError) as excinfo:
            await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    assert "Dachsportal" in str(excinfo.value)


async def test_connection_error_is_friendly() -> None:
    async def drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with CloudClient(
            PORTAL_ID, PASSWORD, base_url=f"http://127.0.0.1:{port}", timeout=5
        ) as client:
            with pytest.raises(ConnectionError) as excinfo:
                await client.read_keys(["Hka_Bd.ulAnzahlStarts"])
    finally:
        server.close()
        await server.wait_closed()
    assert "Dachsportal" in str(excinfo.value)


# --- power demand ---------------------------------------------------------------


async def test_request_power_sends_portal_write_format(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        await client.request_power(2)
    set_requests = [fields for name, fields in portal.requests if name == "set"]
    assert len(set_requests) == 1
    assert json.loads(set_requests[0]["keys"]) == {
        "Stromf_Ew.Anforderung_GLT.bAktiv@1": "1",
        "Stromf_Ew.Anforderung_GLT.bAnzahlModule@1": "2",
    }


async def test_request_power_rejection_is_uncertain(portal: FakePortal) -> None:
    portal.set_reject = True
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        with pytest.raises(WriteError) as excinfo:
            await client.request_power(1)
    assert excinfo.value.outcome_unknown is True
    assert "Nicht erlaubt" in str(excinfo.value)


async def test_request_power_http_failure_is_uncertain(portal: FakePortal) -> None:
    portal.set_status = 500
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        with pytest.raises(HttpError) as excinfo:
            await client.request_power(1)
    assert excinfo.value.status == 500
    assert excinfo.value.outcome_unknown is True
    assert portal.count("set") == 1  # never retried automatically


async def test_request_power_invalid_counts_rejected_before_io(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        for modules in (-1, 11, True, "1", 1.5, None):
            with pytest.raises(ValueError):
                await client.request_power(modules)  # type: ignore[arg-type]
    assert portal.count("set") == 0



async def test_request_run_is_app_equivalent(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        await client.request_run()
    anfordern = [fields for name, fields in portal.requests if name == "dachsAnfordern"]
    assert len(anfordern) == 1
    assert list(anfordern[0]) == ["token"]  # token only, exactly like the app


async def test_request_run_rejection_is_uncertain(portal: FakePortal) -> None:
    portal.set_reject = True
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        with pytest.raises(WriteError) as excinfo:
            await client.request_run()
    assert excinfo.value.outcome_unknown is True


async def test_request_run_via_device_object(portal: FakePortal) -> None:
    async with Dachs.cloud(PORTAL_ID, PASSWORD, base_url=portal.url()) as dachs:
        await dachs.request_run()
    anfordern = [fields for name, fields in portal.requests if name == "dachsAnfordern"]
    assert len(anfordern) == 1
    assert list(anfordern[0]) == ["token"]


# --- extras ------------------------------------------------------------------


async def test_extras_endpoints(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        status = await client.status()
        assert status.device_online is True
        assert status.modules == ("L1",)

        metainfo = await client.metainfo()
        assert metainfo["type"] == "ethernet"

        news = await client.portal_news()
        assert news[0]["titel"] == "News"

        reports = await client.reports()
        assert reports[0]["callcause"] == 4

        profile = await client.daily_profile()
        assert profile["success"] is True
        assert "2025-06-15" in profile["data"]

        schedules = await client.schedules()
        assert schedules["mo"] == [[240, 600]]
        assert schedules["_uhr_"] is False

        statistics = await client.statistics("2025-1", "2025-12")
        assert statistics["ok"] is True
    statistik = [fields for name, fields in portal.requests if name == "statistik"]
    assert statistik[0]["von"] == "2025-1"
    assert statistik[0]["bis"] == "2025-12"
    assert statistik[0]["nummer"] == "0"


async def test_statistics_validates_months(portal: FakePortal) -> None:
    async with CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url()) as client:
        with pytest.raises(ValueError):
            await client.statistics("", "2025-12")
        with pytest.raises(ValueError):
            await client.statistics("2025-1", "  ")
    assert portal.count("statistik") == 0


# --- construction ---------------------------------------------------------------


def test_constructor_validation() -> None:
    for kwargs in (
        {"portal_id": ""},
        {"portal_id": "  "},
        {"portal_id": "85 312"},
        {"module": ""},
        {"module": "@1"},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": float("inf")},
    ):
        parameters: dict = {"portal_id": PORTAL_ID, "password": PASSWORD}
        parameters.update(kwargs)
        with pytest.raises(ValueError):
            CloudClient(**parameters)


async def test_shared_client_is_not_closed_by_device(portal: FakePortal) -> None:
    client = CloudClient(PORTAL_ID, PASSWORD, base_url=portal.url())
    async with Dachs(client=client) as dachs:
        await dachs.update()
        assert dachs.starts == 24989
    assert not client.closed
    await client.close()
