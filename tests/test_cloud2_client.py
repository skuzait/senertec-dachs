"""Loopback tests for the DachsConnect (cloud v2) client.

The fake reproduces the observed service behaviour: the SAML2 SSO
redirect dance with hidden-field capture, the ``init``/``units``/
``canip`` metadata payloads, the datapoint request over websocket with
``age``-flagged frames, and the CanIP write endpoint.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
import aiohttp
from aiohttp import web

from senertec_dachs import (
    AuthenticationError,
    Cloud2CanIpError,
    Cloud2Client,
    Dachs,
    ProtocolError,
    RequestTimeoutError,
    WriteError,
)
from senertec_dachs.cloud2.models import CanIpValue

EMAIL = "dachsportal@senertec.com"
PASSWORD = "demo"
SERIAL = "96385"
OTHER_SERIAL = "97800"

# Session-scoped md5 ids (as the real capture shows: they rotate per
# session; the fake pins one session for determinism).
IDS = {
    "MC000": "aaaaMC00000000000000000000000001",
    "MC001": "aaaaMC00100000000000000000000001",
    "MC002": "aaaaMC00200000000000000000000001",
    "MC003": "aaaaMC00300000000000000000000001",
    "MM000": "aaaaMM00000000000000000000000001",
    "MM001": "aaaaMM00100000000000000000000001",
    "MM004": "aaaaMM00400000000000000000000001",
    "AM027": "aaaaAM02700000000000000000000001",
    "MM011": "aaaaMM01100000000000000000000001",
    "MM025": "aaaaMM02500000000000000000000001",
    "MP033": "aaaaMP03300000000000000000000001",
    "NP190": "aaaaNP19000000000000000000000001",
    "BM001": "aaaaBM00100000000000000000000001",
    "AM029": "aaaaAM02900000000000000000000001",
}
META = {
    "MC000": dict(gain=1, unit="Hours", obdClass="Counter", typeName="MetaDataPointU32", java_type="java.lang.Long", signed=False, array=False, readonly=False, enumName=None),
    "MC001": dict(gain=1, unit="", obdClass="Counter", typeName="MetaDataPointU32", java_type="java.lang.Long", signed=False, array=False, readonly=False, enumName=None),
    "MC002": dict(gain=0.001, unit="kWh", obdClass="Counter", typeName="MetaDataPointU32", java_type="java.lang.Long", signed=False, array=False, readonly=False, enumName=None),
    "MC003": dict(gain=0.001, unit="kWh", obdClass="Counter", typeName="MetaDataPointU32", java_type="java.lang.Long", signed=False, array=False, readonly=False, enumName=None),
    "MM000": dict(gain=1, unit="W", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=False, readonly=True, enumName=None),
    "MM001": dict(gain=0.1, unit="Rpm", obdClass="Signal", typeName="MetaDataPointI32", java_type="java.lang.Integer", signed=True, array=False, readonly=True, enumName=None),
    "MM004": dict(gain=0.1, unit="Hours", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=False, readonly=True, enumName=None),
    "AM027": dict(gain=0.01, unit="°C", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=False, readonly=False, enumName=None),
    "MM011": dict(gain=0.01, unit="°C", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=False, readonly=True, enumName=None),
    "MM025": dict(gain=0.01, unit="°C", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=False, readonly=True, enumName=None),
    "MP033": dict(gain=1, unit="Hours", obdClass="Parameter", typeName="MetaDataPointU32", java_type="java.lang.Long", signed=False, array=False, readonly=False, enumName=None),
    "NP190": dict(gain=0, unit=None, obdClass="Parameter", typeName="MetaDataPointEnum", java_type="java.lang.Short", signed=False, array=False, readonly=False, enumName="OffOn"),
    "BM001": dict(gain=0.01, unit="°C", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=True, readonly=False, enumName=None),
    "AM029": dict(gain=10, unit="W", obdClass="Signal", typeName="MetaDataPointI16", java_type="java.lang.Short", signed=True, array=True, readonly=False, enumName=None),
}

LIVE_VALUES = {
    "MC000": 12657,
    "MC001": 981,
    "MC002": 38352,
    "MC003": 141000,
    "MM000": 5500,
    "MM001": 850,
    "MM004": 126,
    "AM027": 825,
    "MM011": 4550,
    "MM025": 6800,
    "MP033": 12041,
    "NP190": 1,
}

UNIT_PAYLOAD = {
    "connected": True,
    "online": True,
    "mehrmodul": False,
    "leitregler": False,
    "external": False,
    "seriennummer": SERIAL,
    "msrTyp": 3,
    "warnings": [],
    "inbetriebnahmeDatum": 1545519600000,
    "productGroup": "dachsII",
    "artikelNummer": "8000.013.000",
    "benennung": "Dachs Gen2 F5.5",
    "standortName": "Demo",
    "standortPlz": "00000",
    "standortOrt": "Demoland",
    "standortAdresse": "Demo Street 1",
    "standortAnsprech": "Demo Person",
    "module_nr": -1,
    "hasVertrag": True,
    "boards": [
        {
            "name": "CU-CHP01",
            "attributes": [
                IDS["MC000"], IDS["MC001"], IDS["MC002"], IDS["MC003"],
                IDS["MM000"], IDS["MM001"], IDS["MM004"], IDS["AM027"],
                IDS["MM011"], IDS["MM025"], IDS["MP033"], IDS["NP190"],
                IDS["BM001"],
            ],
            "canipErrors": [],
        },
        {
            "name": "EM-CHP01",
            "attributes": [IDS["AM029"], IDS["BM001"]],
            "canipErrors": [
                {
                    "currentError": True,
                    "errorCode": {"category": 16, "code": 96},
                    "timestamp": 1745644392000,
                    "counter": 3,
                    "valid": True,
                    "boardName": "EM-CHP01",
                    "codeText": "H.16.096",
                }
            ],
        },
    ],
    "charts": {"charts": {"ChartChpActivity": {"name": "ChartChpActivity"}}},
}


def _init_payload() -> dict:
    metadata = {}
    for code, entry in META.items():
        metadata[IDS[code]] = {
            "name": IDS[code],
            "virtual": False,
            "bitLength": -1,
            "length": -1,
            "obdClass": entry["obdClass"],
            "typeName": entry["typeName"],
            "java_type": None,  # ignored by the parser
            "type": entry["java_type"],
            "signed": entry["signed"],
            "gain": entry["gain"],
            "unit": entry["unit"],
            "friendlyName": code,
            "array": entry["array"],
            "readonly": entry["readonly"],
            "enumName": entry["enumName"],
            "options": None,
            "bigEndian": False,
        }
    return {
        "app": {"version": "v2.9-4-g968dc76"},
        "metaDataPoints": metadata,
        "translations": {
            "en-gb": {
                "metaDataPoints": {"translations": {
                    IDS["MC000"]: "Total operating hours",
                    IDS["MC001"]: "Number of starts",
                    IDS["MC002"]: "Electrical energy",
                    IDS["MC003"]: "Thermal energy",
                    IDS["MM000"]: "Active power",
                    IDS["MM001"]: "Engine speed",
                    IDS["MM004"]: "Runtime since last start",
                    IDS["AM027"]: "Outdoor temperature",
                    IDS["MM011"]: "CHP inlet temperature",
                    IDS["MM025"]: "CHP outlet temperature 1",
                    IDS["MP033"]: "Hours at last maintenance",
                    IDS["NP190"]: "Second heat generator request",
                    IDS["BM001"]: "Buffer temperature",
                    IDS["AM029"]: "Active power (meter)",
                }},
                "enums": [
                    {"name": "OffOn", "translations": {"0": "Off", "1": "On"}}
                ],
                "errorCategories": {
                    "16": {
                        "translationMedium": "Engine/Inverter",
                        "translations": {"96": "Fuel valve lost"},
                    }
                },
            }
        },
    }


class FakeDachsConnect:
    def __init__(self) -> None:
        self.login_attempts: list[tuple[str, dict[str, str]]] = []
        self.request_payloads: list[dict] = []
        self.write_payloads: list[dict] = []
        self.acs_calls = 0
        self.fail_credentials = False
        self.stale_for: set[str] = set()  # codes answered only with age>0
        self.silent_for: set[str] = set()  # codes with no frame at all
        #: codes whose live frame is delayed; a cached frame (age>0)
        #: arrives immediately so default reads can finish early.
        self.slow_live_for: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.app = web.Application()
        self.app.router.add_get("/rest/saml2/login", self.saml_login_start)
        self.app.router.add_post("/rest/saml2/acs", self.saml_acs)
        self.app.router.add_get("/rest/info/init", self.rest_init)
        self.app.router.add_post("/rest/info/units", self.rest_units)
        self.app.router.add_get("/rest/canip/{serial}", self.rest_canip)
        self.app.router.add_get("/rest/canip/{serial}/disconnect", self.rest_disconnect)
        self.app.router.add_get("/rest/saml2/logout", self.rest_logout)
        self.app.router.add_post("/rest/canip/{serial}/request", self.rest_request)
        self.app.router.add_post("/rest/canip/{serial}/write", self.rest_write)
        self.app.router.add_post("/rest/charts/{serial}/data", self.rest_chart)
        self.app.router.add_get("/ws", self.websocket)
        self._ws: web.WebSocketResponse | None = None
        self._frame_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.deny_write = False
        #: Next /request POST answers 401 once (expired session).
        self.expire_next_request = False

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = runner.addresses[0][1]
        self._runner = runner

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._runner.cleanup()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- SAML SSO ------------------------------------------------------------

    async def saml_login_start(self, request: web.Request) -> web.Response:
        # Redirect to the (same-host-faked) IdP login form.
        raise web.HTTPFound(
            self.url() + "/simplesaml/module.php/core/loginuserpass.php"
            "?AuthState=_42state42"
        )

    async def saml_acs(self, request: web.Request) -> web.Response:
        self.acs_calls += 1
        form = dict(await request.post())
        assert "SAMLResponse" in form and "RelayState" in form
        response = web.Response(text="<html>ok</html>")
        response.set_cookie("JSESSIONID", "sessions")
        return response

    # The IdP form itself is served on the same fake host:
    @web.middleware
    async def idp_middleware(self, request: web.Request, handler) -> web.StreamResponse:
        if request.path == "/simplesaml/module.php/core/loginuserpass.php":
            if request.method == "GET":
                html = (
                    '<form><input type="hidden" name="AuthState" value="_42state42">'
                    '<input type="hidden" name="cookieTime" value="17942">'
                    '<input name="username"><input name="password"></form>'
                )
                return web.Response(text=html)
            form = dict(await request.post())
            self.login_attempts.append(("idp", form))
            if self.fail_credentials or form.get("username") != EMAIL or form.get("password") != PASSWORD:
                return web.Response(text="<html>login failed</html>")
            html = (
                '<form action="acs"><input type="hidden" name="SAMLResponse" '
                'value="{%22demo%22:%20true}">'
                '<input type="hidden" name="RelayState" value="/rest/saml2/login"></form>'
            )
            return web.Response(text=html)
        return await handler(request)

    # --- REST ------------------------------------------------------------------

    async def rest_init(self, request: web.Request) -> web.Response:
        return web.json_response(_init_payload())

    async def rest_units(self, request: web.Request) -> web.Response:
        payload = dict(json.loads(await request.text()))
        assert payload == {"limit": 100, "offset": 0, "filter": {}}
        return web.json_response(
            {
                "units": [
                    dict(UNIT_PAYLOAD, seriennummer=SERIAL, connected=True),
                    dict(UNIT_PAYLOAD, seriennummer=OTHER_SERIAL, connected=False),
                ]
            }
        )

    async def rest_canip(self, request: web.Request) -> web.Response:
        if request.match_info["serial"] != SERIAL:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response(UNIT_PAYLOAD)

    async def rest_disconnect(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def rest_logout(self, request: web.Request) -> web.Response:
        assert request.path == "/rest/saml2/logout"
        if "redirect=/" not in request.query_string:
            return web.json_response({"message": "bad redirect"}, status=404)
        return web.Response(status=200, text="<html>bye</html>")

    async def rest_request(self, request: web.Request) -> web.Response:
        payload = json.loads(await request.text())
        self.request_payloads.append(payload)
        if self.expire_next_request:
            self.expire_next_request = False
            return web.json_response({"message": "session expired"}, status=401)
        for key in payload["keys"]:
            code = next(c for c, md5 in IDS.items() if key.endswith(md5))
            if code in self.silent_for:
                continue
            sample = LIVE_VALUES.get(code)
            if code == "BM001":
                for index in (0, 1, 2):
                    if index != 2 or code not in self.stale_for:
                        await self._frame_queue.put(
                            {"action": "CanipValue",
                             "data": {"sn": SERIAL, "dataPointName": IDS[code],
                                       "value": 55 - index * 5, "age": 0,
                                       "array": True, "index": index,
                                       "boardName": "CU-CHP01", "size": False}}
                        )
                continue
            if code == "AM029":
                for index in (0, 1):
                    await self._frame_queue.put(
                        {"action": "CanipValue",
                          "data": {"sn": SERIAL, "dataPointName": IDS[code],
                                    "value": 550 - index * 100, "age": 0,
                                    "array": True, "index": index,
                                    "boardName": "EM-CHP01", "size": False}}
                    )
                continue
            if code in self.stale_for:
                age = 42 if code in self.stale_for else 0
                await self._frame_queue.put(
                    {"action": "CanipValue",
                      "data": {"sn": SERIAL, "dataPointName": IDS[code],
                                "value": sample, "age": age, "array": False,
                                "index": None, "boardName": "CU-CHP01",
                                "size": False}}
                )
                continue
            if code in self.slow_live_for:
                delay = self.slow_live_for[code]
                async def _send_live_later() -> None:
                    await asyncio.sleep(delay)
                    await self._frame_queue.put(
                        {"action": "CanipValue",
                          "data": {"sn": SERIAL, "dataPointName": IDS[code],
                                    "value": sample, "age": 0, "array": False,
                                    "index": None, "boardName": "CU-CHP01",
                                    "size": False}}
                    )
                self._tasks.append(asyncio.create_task(_send_live_later()))
                await self._frame_queue.put(
                    {"action": "CanipValue",
                      "data": {"sn": SERIAL, "dataPointName": IDS[code],
                                "value": sample, "age": 30, "array": False,
                                "index": None, "boardName": "CU-CHP01",
                                "size": False}}
                )
                continue
            await self._frame_queue.put(
                {"action": "CanipValue",
                  "data": {"sn": SERIAL, "dataPointName": IDS[code],
                            "value": sample, "age": 0, "array": False,
                            "index": None, "boardName": "CU-CHP01", "size": False}}
            )
        # one noise frame to prove draining
        await self._frame_queue.put({"action": "Info", "data": {"version": "x"}})
        return web.json_response({"success": True, "errorKeys": []})

    async def rest_write(self, request: web.Request) -> web.Response:
        payload = json.loads(await request.text())
        self.write_payloads.append(payload)
        if self.deny_write:
            return web.json_response({"ok": False}, status=403)
        return web.json_response({"ok": True})

    async def rest_chart(self, request: web.Request) -> web.Response:
        payload = json.loads(await request.text())
        assert payload["chartName"] == "ChartChpActivity"
        assert payload["sn"] == SERIAL
        return web.json_response({"fake": "chart", "from": 1, "to": 2})

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(self._frame_queue.get(), timeout=0.1)
                except (asyncio.TimeoutError, TimeoutError):
                    if ws.closed:
                        return ws
                    try:
                        message = await ws.receive(timeout=0.05)
                    except (asyncio.TimeoutError, TimeoutError):
                        continue
                    if message.type in (
                        aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                    ):
                        return ws
                    continue
                if frame == "__close__":
                    await ws.close()
                    return ws
                if self._ws is not ws:
                    # A newer connection took over (client reconnect):
                    # give the frame back so the current connection
                    # serves it.
                    self._frame_queue.put_nowait(frame)
                    return ws
                await ws.send_str(json.dumps(frame))
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            return ws


@pytest.fixture
async def connect_service() -> FakeDachsConnect:
    fake = FakeDachsConnect()
    fake.app.middlewares.append(fake.idp_middleware)  # type: ignore[arg-type]
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def make_client(service: FakeDachsConnect, **kwargs) -> Cloud2Client:
    return Cloud2Client(
        EMAIL,
        PASSWORD,
        base_url=service.url(),
        sso_url=service.url(),
        ws_url=f"ws://127.0.0.1:{service.port}/ws",
        **kwargs,
    )


async def test_session_cookies_reuse_skips_saml(
    connect_service: FakeDachsConnect,
) -> None:
    """A valid session from cookies skips the whole SAML chain.

    Discriminator: the fast path fetches init during login(), the SAML
    path leaves init untouched until _ensure_login().
    """
    async with make_client(connect_service, serial=SERIAL) as first:
        await first.connect()
        cookies = first.session_cookies()
        assert cookies  # the login produced session cookies

    reconnect = make_client(connect_service, serial=SERIAL, cookies=cookies)
    async with reconnect:
        assert reconnect._init is None
        await reconnect.login()
        assert reconnect._logged_in
        assert reconnect._init is not None  # fetched by the fast path
        assert reconnect._initial_cookies is None


async def test_stale_cookies_fall_back_to_saml(
    connect_service: FakeDachsConnect,
) -> None:
    """Expired session cookies transparently trigger the full login."""
    stale = {"PORTAL2": "definitely-expired", "JSESSIONID": "gone"}
    async with make_client(connect_service, serial=SERIAL, cookies=stale) as client:
        await client.connect()
        assert client._logged_in  # recovered via the SAML chain
        assert client._initial_cookies is None


# --- validation -----------------------------------------------------------------


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        Cloud2Client("", "pw")
    with pytest.raises(ValueError):
        Cloud2Client(EMAIL, "")
    with pytest.raises(ValueError):
        Cloud2Client(EMAIL, "pw", serial="96385@1")
    with pytest.raises(ValueError):
        Cloud2Client(EMAIL, "pw", timeout=0)
    with pytest.raises(ValueError):
        Cloud2Client(EMAIL, "pw", timeout=-1.0)


# --- login -----------------------------------------------------------------------


async def test_login_failure_is_friendly(connect_service: FakeDachsConnect) -> None:
    connect_service.fail_credentials = True
    async with make_client(connect_service) as client:
        with pytest.raises(AuthenticationError):
            await client.login()


# --- units and connect ------------------------------------------------------------


async def test_units_and_multi_serial_selection(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service) as client:
        units = await client.units()
        assert [unit.serial for unit in units] == [SERIAL, OTHER_SERIAL]
        assert units[0].model == "Dachs Gen2 F5.5"
        assert units[0].article_number == "8000.013.000"
        with pytest.raises(ValueError) as error:
            await client.connect()
        assert "serial=..." in str(error.value)


async def test_connect_resolves_catalogue_and_boards(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        assert client.serial == SERIAL
        assert client.boards == ("CU-CHP01", "EM-CHP01")
        assert client.catalogue is not None
        assert set(client._compat_ids) == {
            "Hka_Bd.ulBetriebssekunden",
            "Hka_Bd.ulAnzahlStarts",
            "Hka_Bd.ulArbeitElektr",
            "Hka_Bd.ulArbeitThermHka",
            "Hka_Mw1.sWirkleistung",
            "Hka_Mw1.ulMotorlaufsekunden",
            "Hka_Mw1.usDrehzahl",
            "Hka_Mw1.Temp.sbAussen",
            "Hka_Mw1.Temp.sbGen",
            "Hka_Mw1.Temp.sbVorlauf",
            "Wartung_Cache.ulBetriebssekundenBei",
        }


# --- compat reads (shared facade path) --------------------------------------------


async def test_read_keys_maps_compat_values_exactly(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        values = await client.read_keys(
            [
                "Hka_Bd.ulBetriebssekunden",
                "Hka_Bd.ulAnzahlStarts",
                "Hka_Bd.ulArbeitElektr",
                "Hka_Bd.ulArbeitThermHka",
                "Hka_Mw1.sWirkleistung",
                "Hka_Mw1.ulMotorlaufsekunden",
                "Hka_Mw1.usDrehzahl",
                "Hka_Mw1.Temp.sbAussen",
                "Hka_Mw1.Temp.sbGen",
                "Wartung_Cache.ulBetriebssekundenBei",
                # not answered on cloud v2: MSR2-only keys
                "Hka_Mw1.Temp.sbRuecklauf",
                "Hka_Ew.HydraulikNr.bSpeicherArt",
                "Laufraster15Min_aktTag.bDoppelstunde[0]",
            ]
        )
        assert values["Hka_Bd.ulBetriebssekunden"] == "12657"  # hours (honest)
        assert values["Hka_Bd.ulAnzahlStarts"] == "981"
        assert values["Hka_Bd.ulArbeitElektr"] == "38.352"  # kWh
        assert values["Hka_Bd.ulArbeitThermHka"] == "141"
        assert values["Hka_Mw1.sWirkleistung"] == "5.5"  # 5500 W -> 5.5 kW
        assert values["Hka_Mw1.ulMotorlaufsekunden"] == "12.6"  # hours
        assert values["Hka_Mw1.usDrehzahl"] == "85"  # 850 * 0.1, integral
        assert values["Hka_Mw1.Temp.sbAussen"] == "8.25"
        assert values["Hka_Mw1.Temp.sbGen"] == "45.5"
        assert values["Wartung_Cache.ulBetriebssekundenBei"] == "12041"
        assert "Hka_Mw1.Temp.sbRuecklauf" not in values
        assert "Hka_Ew.HydraulikNr.bSpeicherArt" not in values


async def test_identity_keys_come_from_the_unit_payload(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        values = await client.read_keys(
            [
                "Hka_Bd_Stat.uchSeriennummer",
                "Hka_Bd_Stat.uchTeilenummer",
                "Hka_Bd_Stat.ulInbetriebnahmedatum",
            ]
        )
        assert values["Hka_Bd_Stat.uchSeriennummer"] == SERIAL
        assert values["Hka_Bd_Stat.uchTeilenummer"] == "8000.013.000"
        # 1545519600000 ms epoch -> seconds since 2000 (MSR2 wire semantics)
        assert values["Hka_Bd_Stat.ulInbetriebnahmedatum"] == "598834800"


async def test_request_payloads_use_board_md5_keys(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.read_keys(["Hka_Mw1.sWirkleistung"])
        (payload,) = connect_service.request_payloads
        assert payload["seriennummer"] == SERIAL
        assert payload["keys"] == [f"CU-CHP01.{IDS['MM000']}"]


# --- native datapoint access --------------------------------------------------------


async def test_read_datapoints_native_with_array_and_enum(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.read_datapoints(
            ["MM000", "NP190", "CU-CHP01.BM001[1]", "AM029"]
        )
        # request used one wire key per md5
        (payload,) = connect_service.request_payloads
        assert payload["keys"] == sorted([
            f"CU-CHP01.{IDS['MM000']}",
            f"CU-CHP01.{IDS['NP190']}",
            f"CU-CHP01.{IDS['BM001']}",
            f"EM-CHP01.{IDS['AM029']}",
        ])


async def test_read_datapoints_returns_typed_values(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        values = await client.read_datapoints(
            ["MM000", "MM004", "NP190", "MC002", "CU-CHP01.BM001[2]", "AM029"]
        )
        assert values["MM000"].value == Decimal("5500")  # 5500 W gain 1
        assert values["MM000"].unit == "W"
        assert values["MM004"].value == Decimal("12.6")  # 126 * 0.1 hours
        assert values["NP190"].value == "On"  # enum translation
        assert values["MC002"].value == Decimal("38.352")  # 38352 * 0.001
        assert values["CU-CHP01.BM001[2]"].value == Decimal("0.45")  # 45 * 0.01 °C
        assert values["CU-CHP01.BM001[2]"].board == "CU-CHP01"
        assert values["AM029[0]"].value == Decimal("5500")  # 550 * 10 W
        assert values["AM029[1]"].value == Decimal("4500")  # 450 * 10 W


async def test_unknown_datapoint_raises(connect_service: FakeDachsConnect) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        with pytest.raises(ValueError):
            await client.read_datapoints(["ZZ999"])


async def test_expired_session_recovers_with_relogin(
    connect_service: FakeDachsConnect,
) -> None:
    """A mid-session 401 transparently re-logins, reconnects, retries once."""
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        acs_before = connect_service.acs_calls
        connect_service.expire_next_request = True
        values = await client.read_datapoints(["MM000"])
        assert values["MM000"].value == Decimal("5500")
        # the full SAML chain ran exactly one more time, and the same
        # datapoint request was retried after the recovery
        assert connect_service.acs_calls == acs_before + 1
        assert len(connect_service.request_payloads) == 2
        assert client._logged_in


async def test_unrecoverable_session_raises(
    connect_service: FakeDachsConnect,
) -> None:
    """When the re-login itself fails, the read raises honestly."""
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        connect_service.expire_next_request = True
        connect_service.fail_credentials = True
        with pytest.raises(AuthenticationError):
            await client.read_datapoints(["MM000"])
        # one expired request, no successful retry
        assert len(connect_service.request_payloads) == 1


async def test_read_datapoints_without_codes_reads_everything(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        values = await client.read_datapoints()
        # unique codes answer under their plain code
        assert "MM000" in values
        assert values["MC002"].value == Decimal("38.352")
        assert "MC001" in values
        assert "AM027" in values
        assert values["MM001"].value == Decimal("85")  # 850 * 0.1 Rpm
        # arrays expand per element, board-scoped where duplicated
        assert "CU-CHP01.BM001[1]" in values
        assert "AM029[1]" in values  # AM029 only exists on EM-CHP01
        # the request listed every datapoint of every board once
        (payload,) = connect_service.request_payloads
        assert len(payload["keys"]) == 15  # 14 md5 ids, BM001 hits its md5 once


# --- staleness --------------------------------------------------------------------


async def test_stale_values_answer_reads_and_are_flagged(
    connect_service: FakeDachsConnect,
) -> None:
    connect_service.stale_for = {"MM000"}
    async with make_client(connect_service, serial=SERIAL) as client:
        values = await client.read_keys(["Hka_Mw1.sWirkleistung"])
        assert values["Hka_Mw1.sWirkleistung"] == "5.5"
        assert client.values_stale is True


# --- errors and charts --------------------------------------------------------------


async def test_get_errors_translates_categories(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        errors = await client.get_errors()
        (entry,) = errors
        assert isinstance(entry, Cloud2CanIpError)
        assert entry.board == "EM-CHP01"
        assert entry.code_text == "H.16.096"
        assert entry.translation == "Fuel valve lost"
        assert entry.category_text == "Engine/Inverter"
        assert entry.current is True


async def test_chart_names_and_default_chart(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        names = await client.chart_names()
        assert names == ["ChartChpActivity"]
        body = await client.chart("ChartChpActivity")
        assert body["fake"] == "chart"


async def test_default_read_returns_before_slow_live(
    connect_service: FakeDachsConnect,
) -> None:
    """The default read finishes once cache covers the request."""
    connect_service.slow_live_for = {"MM000": 60.0}  # live value would take 60 s
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        started = asyncio.get_running_loop().time()
        values = await client.read_datapoints(["MM000", "MC002"])
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 5.0  # did not wait for the 60 s live frame
        assert values["MM000"].value == LIVE_VALUES["MM000"]
        assert values["MM000"].age_seconds == 30  # answered from cache
        assert values["MC002"].raw == LIVE_VALUES["MC002"]  # gain 0.001 applied
        assert client.values_stale is True


async def test_wait_for_live_holds_out_for_live_answers(
    connect_service: FakeDachsConnect,
) -> None:
    """wait_for_live=True keeps the request open for live values."""
    connect_service.slow_live_for = {"MM000": 0.5}
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        started = asyncio.get_running_loop().time()
        values = await client.read_datapoints(["MM000"], wait_for_live=True)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.4  # held the request open for the live frame
        assert values["MM000"].value == LIVE_VALUES["MM000"]
        assert values["MM000"].age_seconds == 0  # live, not the cached copy
        assert client.values_stale is False


async def test_values_carry_server_display_names(
    connect_service: FakeDachsConnect,
) -> None:
    """Readings expose the portal's translation-table name."""
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        values = await client.read_datapoints(["MM000", "MC002"])
        assert values["MM000"].name == "Active power"
        assert values["MC002"].name == "Electrical energy"


async def test_display_names_api(connect_service: FakeDachsConnect) -> None:
    """display_names() returns the portal table; unknown languages fail."""
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        names = client.display_names()
        assert names[IDS["MM000"]] == "Active power"
        assert names[IDS["AM027"]] == "Outdoor temperature"
        with pytest.raises(ValueError):
            client.display_names(language="fr-fr")


async def test_listener_pushes_values_per_frame(
    connect_service: FakeDachsConnect,
) -> None:
    """The listener converts each value frame and invokes the callback."""
    received: list[CanIpValue] = []
    done = asyncio.Event()

    def on_value(value: CanIpValue) -> None:
        received.append(value)
        if len(received) >= 2:
            done.set()

    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        client.start_listener(on_value, codes=["MM000", "MC002"], interval=60.0)
        try:
            await asyncio.wait_for(done.wait(), timeout=5.0)
        finally:
            client.stop_listener()
        codes = {value.code for value in received}
        assert codes == {"MM000", "MC002"}
        assert all(value.name for value in received)
        # stop_listener cancels the task even though the read would
        # still wait for more frames (interval=60 s)
        assert client._listener_task is None or client._listener_task.done()


async def test_listener_rejects_double_start(
    connect_service: FakeDachsConnect,
) -> None:
    """Only one listener can run per client."""
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        client.start_listener(lambda value: None, codes=["MM000"], interval=60.0)
        try:
            with pytest.raises(RuntimeError):
                client.start_listener(lambda value: None, codes=["MM000"], interval=60.0)
        finally:
            client.stop_listener()


# --- writes and unavailable facade calls ------------------------------------------


async def test_write_datapoint_sends_board_and_md5_value_map(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        await client.write_datapoint("NP190", 1)
        (payload,) = connect_service.write_payloads
        assert payload == {"boardName": "CU-CHP01", "values": {IDS["NP190"]: 1}}


async def test_write_rejection_is_uncertain(connect_service: FakeDachsConnect) -> None:
    connect_service.deny_write = True
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        with pytest.raises(WriteError) as error:
            await client.write_datapoint("NP190", 1)
        assert error.value.outcome_unknown is True


async def test_request_run_and_power_are_honest_failures(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        with pytest.raises(WriteError) as error:
            await client.request_run()
        assert error.value.outcome_unknown is False
        with pytest.raises(WriteError) as error:
            await client.request_power(2)
        assert error.value.outcome_unknown is False


async def test_logout_uses_the_spa_endpoint(
    connect_service: FakeDachsConnect,
) -> None:
    async with make_client(connect_service, serial=SERIAL) as client:
        await client.connect()
        await client.logout()
        assert client.serial is None
        assert client._unit is None


# --- device object integration -------------------------------------------------------


async def test_device_cloud2_backend_read_e2e(
    connect_service: FakeDachsConnect,
) -> None:
    client = make_client(connect_service, serial=SERIAL)
    try:
        async with Dachs(client=client) as dachs:
            assert isinstance(dachs.client, Cloud2Client)
            snapshot = await dachs.update()
            assert dachs.serial_number == SERIAL
            assert dachs.part_number == "8000.013.000"
            assert dachs.commissioned_epoch_seconds == 598834800
            assert dachs.electrical_energy_kwh == Decimal("38.352")
            assert dachs.thermal_energy_kwh == Decimal("141")
            assert dachs.power_w == 5500
            assert dachs.engine_runtime_hours == Decimal("12.6")
            assert dachs.runtime_hours == Decimal("12657")
            assert dachs.starts == 981
            assert dachs.last_maintenance_runtime_hours == Decimal("12041")
            assert dachs.outdoor_temperature == Decimal("8.25")
            # MSR2-only things are honestly absent:
            assert dachs.fault_code is None
            assert dachs.heating_circuit_1_pump is None
            assert snapshot.raw.get("Hka_Mw1.sWirkleistung") == "5.5"
            assert dachs.device_online is True
    finally:
        # The device does not own the caller-provided client.
        await client.close()
