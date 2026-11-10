"""Asynchronous client for DachsConnect (DachsPortal2, cloud v2).

This is the cloud backend for latest-generation Dachs (MSR3 controller,
product group ``dachsII`` and related units). Login is SAML2 SSO: the
service provider (``dachsconnect.senertec.com``) redirects to the
SimpleSAMLphp IdP, the credentials are posted to the IdP login form
(its hidden fields are forwarded so the flow survives AuthState
renames), and the returned SAML response is posted to the SP's
assertion consumer service. The session lives in cookies.

One client maintains one **connected unit**; accounts with several
units select one explicitly with ``serial=...`` or later via
:meth:`switch_unit`. After a connect, values arrive over the websocket
following a ``/rest/canip/{serial}/request`` POST; the socket stays
open and is re-used. ``age`` 0 frames are live values; ``age`` > 0
frames are the portal's cached last poll — they still answer reads and
are reported as stale (see :attr:`values_stale`), mirroring the
first-generation portal's freshness honesty.

Writes: the documented write channel is the CanIP datapoint write
(:meth:`write_datapoint`, untested against a real account — treat the
first use as an experiment and never retry blindly). This backend has
no one-shot run request and no verified external power-demand write;
the corresponding facade calls raise with an honest message instead.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import aiohttp

from .._reader import SnapshotReaderMixin, normalize_requested_keys
from ..errors import (
    AuthenticationError,
    ConnectionError,
    DachsError,
    HttpError,
    ProtocolError,
    RequestTimeoutError,
    WriteError,
)
from .catalog import (
    Catalogue,
    COMPAT_BY_KEY,
    build_catalogue,
    compat_resolution,
)
from .models import CanIpError, CanIpValue, Cloud2Status, UnitInfo
from .protocol import (
    BASE_URL,
    MetaDataPoint,
    SSO_URL,
    WS_URL,
    apply_gain,
    parse_json,
    to_wire_text,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_LANG = "en-gb"

_EPOCH_2000_SECONDS = 946684800
_ACTION_VALUE = "CanipValue"
_ACTION_STORE = "HkaStore"


class _HiddenInputParser(HTMLParser):
    """Collect hidden input fields of an SSO page."""

    def __init__(self) -> None:
        super().__init__()
        self.hidden_fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if (
            tag == "input"
            and attributes.get("type") == "hidden"
            and isinstance(attributes.get("name"), str)
            and isinstance(attributes.get("value"), str)
        ):
            self.hidden_fields[attributes["name"]] = attributes["value"]


@dataclass(frozen=True, slots=True)
class _Plan:
    """One requested datapoint (scalar, or one array code/element)."""

    key: str  # requested name: "MM000", "CU-CHP01.MM000", "BM001[3]"
    code: str
    board: str
    point: MetaDataPoint
    index: int | None  # explicit array element plan


def _mask_serial(serial: str | None) -> str:
    if serial is None:
        return "?"
    return f"{serial[:3]}XXX"


def _gain_decimal(gain: Any) -> Decimal | None:
    if not isinstance(gain, (int, float, Decimal)):
        return None
    as_decimal = Decimal(str(gain))
    return None if as_decimal == 0 else as_decimal


def _enum_translation(
    init: Mapping[str, Any] | None, language: str, enum_name: str, raw: int
) -> str | None:
    if init is None:
        return None
    translations = init.get("translations")
    if not isinstance(translations, Mapping):
        return None
    language_block = translations.get(language)
    if not isinstance(language_block, Mapping):
        return None
    enums = language_block.get("enums")
    if not isinstance(enums, (list, Mapping)):
        return None
    if isinstance(enums, list):
        for entry in enums:
            if isinstance(entry, Mapping) and entry.get("name") == enum_name:
                mapping = entry.get("translations")
                if isinstance(mapping, Mapping):
                    return mapping.get(str(raw))
        return None
    entry = enums.get(enum_name)
    if isinstance(entry, Mapping):
        mapping = entry.get("translations", entry)
        return mapping.get(str(raw)) if isinstance(mapping, Mapping) else None
    return None


def _parse_unit_entry(entry: Mapping[str, Any]) -> UnitInfo:
    serial = entry.get("seriennummer")
    if not isinstance(serial, str) or serial == "":
        raise ProtocolError("A unit entry of the account has no serial number")

    def _optional_text(field: str) -> str | None:
        return entry.get(field) if isinstance(entry.get(field), str) else None

    def _optional_bool(field: str) -> bool | None:
        return entry.get(field) if isinstance(entry.get(field), bool) else None

    return UnitInfo(
        serial=serial,
        model=_optional_text("benennung") or "",
        product_group=_optional_text("productGroup") or "",
        online=_optional_bool("online"),
        connected=_optional_bool("connected"),
        mehrmodul=_optional_bool("mehrmodul"),
        leitregler=_optional_bool("leitregler"),
        external=_optional_bool("external"),
        article_number=_optional_text("artikelNummer"),
        commissioned_ms=(
            entry.get("inbetriebnahmeDatum")
            if isinstance(entry.get("inbetriebnahmeDatum"), int)
            else None
        ),
        location_name=_optional_text("standortName"),
        location_city=_optional_text("standortOrt"),
        location_postal_code=_optional_text("standortPlz"),
        location_street=_optional_text("standortAdresse"),
        contact_person=_optional_text("standortAnsprech"),
    )


def _canip_value_from_frame(
    board: str,
    code: str,
    point: MetaDataPoint,
    data: Mapping[str, Any],
    init: Mapping[str, Any] | None,
    language: str,
) -> CanIpValue:
    """Build one :class:`CanIpValue` from a websocket value frame."""
    raw = data.get("value")
    age = data.get("age")
    age_seconds = age if isinstance(age, int) and age >= 0 else 0
    gain = _gain_decimal(point.gain)
    enum_name = point.enum_name
    value: Decimal | str | bool | None
    if isinstance(raw, bool):
        value = raw
    elif raw is None:
        value = None
    elif enum_name and isinstance(raw, int):
        value = _enum_translation(init, language, enum_name, raw) or str(raw)
    elif isinstance(raw, (int, float, Decimal)):
        value = apply_gain(raw, point.gain) if gain is not None else (
            raw if isinstance(raw, Decimal) else Decimal(str(raw))
        )
    elif isinstance(raw, str):
        value = raw
    else:
        raise ProtocolError(
            f"CanIP value for {board}.{code} has an unsupported wire type: {type(raw).__name__}"
        )
    return CanIpValue(
        code=code,
        board=board,
        id=point.id,
        raw=raw,
        value=value,
        unit=point.unit,
        gain=gain,
        enum_name=enum_name,
        age_seconds=age_seconds,
        name=None,
    )


class Cloud2Client(SnapshotReaderMixin):
    """One DachsConnect session for one account (one connected unit)."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        serial: str | None = None,
        language: str = DEFAULT_LANG,
        base_url: str = BASE_URL,
        sso_url: str = SSO_URL,
        ws_url: str = WS_URL,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not isinstance(email, str) or not email.strip():
            raise ValueError("email must be a non-empty string")
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string")
        if isinstance(serial, str) and "@" in serial:
            raise ValueError("serial must be the plain serial number")
        if not isinstance(language, str) or not language:
            raise ValueError("language must be a tag like 'en-gb' or 'de-de'")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number of seconds")
        if timeout <= 0 or timeout != timeout or timeout in (float("inf"), float("-inf")):
            raise ValueError("timeout must be a positive finite number of seconds")
        self._email = email
        self._password = password
        self._requested_serial = serial
        self._language = language
        self._base = base_url.rstrip("/")
        self._sso = sso_url.rstrip("/")
        self._ws_url = ws_url
        self._timeout_seconds = float(timeout)
        self._client_timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._session = session
        self._owns_session = session is None
        self._closed = False
        self._logged_in = False
        self._serial: str | None = None
        self._init: dict[str, Any] | None = None
        self._unit: dict[str, Any] | None = None
        self._catalogue: Catalogue | None = None
        self._compat_ids: dict[str, str] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._ws_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._last_status: Cloud2Status | None = None
        self._values_stale: bool | None = None
        self._lost_connection = False

    # --- lifecycle -------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_status(self) -> Cloud2Status | None:
        """Recorded by the last connect: unit online state, boards."""
        return self._last_status

    @property
    def values_stale(self) -> bool | None:
        """Whether the last read answered with cached (``age`` > 0) values."""
        return self._values_stale

    @property
    def serial(self) -> str | None:
        """Serial of the connected unit (``None`` before connecting)."""
        return self._serial

    @property
    def boards(self) -> tuple[str, ...]:
        """Board names of the connected unit."""
        if self._unit is None:
            raise RuntimeError("No unit connected yet")
        return tuple(
            board["name"]
            for board in self._unit.get("boards", [])
            if isinstance(board, Mapping) and board.get("name")
        )

    @property
    def catalogue(self) -> Catalogue | None:
        """Resolved datapoint catalogue (available after connecting)."""
        return self._catalogue

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._session

    async def close(self) -> None:
        """Close the client. Idempotent; only owned sessions are closed."""
        if self._closed:
            return
        self._closed = True
        await self._shutdown_ws()
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None
        self._logged_in = False
        self._serial = None
        self._unit = None
        self._catalogue = None
        self._compat_ids = {}

    async def __aenter__(self) -> Cloud2Client:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- HTTP plumbing --------------------------------------------------------

    async def _get(self, path: str) -> tuple[int, str]:
        session = self._ensure_session()
        try:
            async with session.get(
                self._base + path, timeout=self._client_timeout
            ) as response:
                return response.status, await response.text()
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"DachsConnect did not answer within {self._timeout_seconds} "
                f"seconds for GET {path}"
            ) from error
        except aiohttp.ClientConnectionError as error:
            raise ConnectionError(f"Could not reach DachsConnect: {error}") from error
        except aiohttp.ClientError as error:
            raise ConnectionError(f"Request to DachsConnect failed: {error}") from error

    async def _post(self, path: str, payload: Mapping[str, Any] | str) -> tuple[int, str]:
        """POST a JSON body (dict → JSON, str → verbatim)."""
        session = self._ensure_session()
        headers = {"Content-Type": "application/json"}
        body = json.dumps(payload) if not isinstance(payload, str) else payload
        try:
            async with session.post(
                self._base + path,
                data=body,
                headers=headers,
                timeout=self._client_timeout,
            ) as response:
                return response.status, await response.text()
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"DachsConnect did not answer within {self._timeout_seconds} "
                f"seconds for POST {path}"
            ) from error
        except aiohttp.ClientConnectionError as error:
            raise ConnectionError(f"Could not reach DachsConnect: {error}") from error
        except aiohttp.ClientError as error:
            raise ConnectionError(f"Request to DachsConnect failed: {error}") from error

    async def _get_url(self, url: str) -> tuple[int, str]:
        session = self._ensure_session()
        try:
            async with session.get(url, timeout=self._client_timeout) as response:
                return response.status, await response.text()
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"the SSO server did not answer within {self._timeout_seconds} seconds"
            ) from error
        except aiohttp.ClientError as error:
            raise ConnectionError(f"Could not reach the SSO server: {error}") from error

    async def _post_form(self, url: str, fields: Mapping[str, str]) -> tuple[int, str]:
        session = self._ensure_session()
        try:
            async with session.post(
                url, data=dict(fields), timeout=self._client_timeout
            ) as response:
                return response.status, await response.text()
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"the SSO server did not answer within {self._timeout_seconds} seconds"
            ) from error
        except aiohttp.ClientError as error:
            raise ConnectionError(f"Could not reach the SSO server: {error}") from error

    # --- SAML SSO login --------------------------------------------------------

    async def login(self) -> None:
        """Perform the SAML2 SSO login. Idempotent; needed before reads."""
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._logged_in:
            return
        status, idp_page = await self._get_url(f"{self._base}/rest/saml2/login")
        if status != 200:
            raise AuthenticationError(
                f"Starting the SSO login failed (HTTP {status}); the login "
                f"server may be unavailable."
            )
        idp_form = _HiddenInputParser()
        idp_form.feed(idp_page)
        if "AuthState" not in idp_form.hidden_fields:
            raise AuthenticationError(
                "The SSO login page did not carry its state fields; the "
                "login server may be unavailable."
            )
        status, response_html = await self._post_form(
            f"{self._sso}/simplesaml/module.php/core/loginuserpass.php",
            {
                **idp_form.hidden_fields,
                "username": self._email,
                "password": self._password,
            },
        )
        assertion_form = _HiddenInputParser()
        assertion_form.feed(response_html)
        saml_response = assertion_form.hidden_fields.get("SAMLResponse")
        relay_state = assertion_form.hidden_fields.get("RelayState")
        if not saml_response or not relay_state:
            raise AuthenticationError(
                "The SSO server rejected the email or password."
            )
        status, _ = await self._post_form(
            f"{self._base}/rest/saml2/acs",
            {"SAMLResponse": saml_response, "RelayState": relay_state},
        )
        if status != 200:
            raise AuthenticationError(
                f"Completing the SSO login failed (HTTP {status})."
            )
        self._logged_in = True

    async def _ensure_login(self) -> None:
        if not self._logged_in:
            await self.login()
        if self._init is None:
            status, text = await self._get("/rest/info/init")
            if status != 200:
                raise HttpError(
                    f"DachsConnect returned HTTP {status} for init", status=status
                )
            init = parse_json(text)
            if not isinstance(init, dict) or not isinstance(
                init.get("metaDataPoints"), Mapping
            ):
                raise ProtocolError("The init payload had an unexpected shape")
            self._init = init

    # --- unit management -------------------------------------------------------

    async def units(self) -> list[UnitInfo]:
        """All units of the account."""
        await self._ensure_login()
        status, text = await self._post(
            "/rest/info/units", {"limit": 100, "offset": 0, "filter": {}}
        )
        if status != 200:
            raise HttpError(
                f"DachsConnect returned HTTP {status} for the unit list",
                status=status,
            )
        body = parse_json(text)
        if not isinstance(body, dict) or not isinstance(body.get("units"), list):
            raise ProtocolError("The unit list response had an unexpected shape")
        return [_parse_unit_entry(entry) for entry in body["units"]]

    async def connect(self) -> None:
        """Log in and connect the selected unit (resolves the catalogue)."""
        await self._ensure_login()
        await self._select_unit()
        assert self._serial is not None
        status, text = await self._get(
            f"/rest/canip/{_path_segment(self._serial)}"
        )
        if status != 200:
            raise HttpError(
                f"DachsConnect returned HTTP {status} connecting unit "
                f"{_mask_serial(self._serial)}",
                status=status,
            )
        unit = parse_json(text)
        if not isinstance(unit, dict) or not isinstance(unit.get("boards"), list):
            raise ProtocolError("The unit payload had an unexpected shape")
        self._unit = unit
        assert self._init is not None
        self._catalogue = build_catalogue(self._init, unit)
        self._compat_ids = compat_resolution(self._catalogue)
        self._last_status = Cloud2Status.from_unit(unit)

    async def disconnect(self) -> None:
        """Disconnect the connected unit and close its websocket."""
        if self._serial is None:
            return
        await self._shutdown_ws()
        await self._get(f"/rest/canip/{_path_segment(self._serial)}/disconnect")
        self._serial = None
        self._unit = None
        self._catalogue = None
        self._compat_ids = {}

    async def logout(self) -> None:
        """End the portal session (SPA-observed logout endpoint).

        Closes the websocket first; the HTTP session cookies expire
        server-side. Does not close owned sessions.
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        await self._shutdown_ws()
        await self._get("/rest/saml2/logout?redirect=/")
        self._logged_in = False
        self._serial = None
        self._unit = None
        self._catalogue = None
        self._compat_ids = {}

    async def switch_unit(self, serial: str) -> None:
        """Connect a different unit of the account by serial number."""
        if not isinstance(serial, str) or not serial or "@" in serial:
            raise ValueError("serial must be the plain serial number")
        if self._closed:
            raise RuntimeError("Client is closed")
        await self.disconnect()
        self._requested_serial = serial
        await self.connect()

    async def _select_unit(self) -> None:
        available = await self.units()
        if self._requested_serial is not None:
            if self._requested_serial not in {unit.serial for unit in available}:
                raise ValueError(
                    f"Serial {self._requested_serial!r} is not part of this "
                    f"account ({len(available)} unit(s))"
                )
            self._serial = self._requested_serial
            return
        if not available:
            raise ConnectionError(
                "This DachsConnect account has no units; a unit must exist "
                "before connecting."
            )
        if len(available) > 1:
            raise ValueError(
                f"This DachsConnect account has {len(available)} units; pass "
                f"serial=... (units: {[unit.serial for unit in available]})"
            )
        self._serial = available[0].serial

    # --- websocket -------------------------------------------------------------

    async def _ensure_ws(self) -> None:
        if self._ws is not None and not self._ws.closed:
            return
        session = self._ensure_session()
        try:
            ws = await session.ws_connect(
                self._ws_url,
                timeout=aiohttp.ClientWSTimeout(
                    ws_receive=None, ws_close=2.0
                ),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise ConnectionError(
                f"Could not open the DachsConnect websocket: {error}"
            ) from error
        self._ws = ws
        self._lost_connection = False
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    continue
                try:
                    frame = parse_json(message.data)
                except ProtocolError:
                    continue
                if isinstance(frame, Mapping):
                    await self._ws_queue.put(dict(frame))
            self._lost_connection = True
        except asyncio.CancelledError:  # pragma: no cover — cancellations only
            raise

    async def _shutdown_ws(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._pump_task = None
        if self._ws is not None and not self._ws.closed:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
            except (
                asyncio.TimeoutError,
                TimeoutError,
                aiohttp.ClientError,
                ConnectionResetError,
            ):
                # The close handshake is best effort; the transport dies
                # with the underlying session.
                pass
        self._ws = None

    async def _next_value_frame(self) -> dict[str, Any]:
        """The next CanipValue frame; all other actions are drained."""
        while True:
            if self._ws is None or self._ws.closed:
                raise ConnectionError(
                    "The DachsConnect websocket closed before the values "
                    "arrived"
                )
            frame = await self._ws_queue.get()
            action = frame.get("action")
            if action == _ACTION_VALUE:
                data = frame.get("data")
                if not isinstance(data, Mapping) or not isinstance(
                    data.get("dataPointName"), str
                ):
                    continue
                if data.get("size") is True:
                    continue  # array-size indicator, not a value
                return frame
            if action == _ACTION_STORE:
                data = frame.get("data")
                if isinstance(data, Mapping) and data.get("updateType") == "remove":
                    self._lost_connection = True
                continue

    async def _collect_frames(
        self, plans: Sequence[_Plan]
    ) -> tuple[dict[str, Mapping[str, Any]], bool]:
        """POST the datapoint request and await the websocket answers.

        Returns ``{md5: data}`` — live answers when available and cached
        (`age > 0`) answers as fallback — plus whether any fallback was
        used. Unanswered md5 ids are absent, which callers surface as
        missing values.
        """
        wire_keys: dict[str, str] = {}
        for plan in plans:
            wire_key = f"{plan.board}.{plan.point.id}"
            if (
                wire_key in wire_keys
                and wire_keys[wire_key] != _frame_selector(plan)
            ):
                raise ProtocolError(
                    f"Conflicting requests for {wire_key}: "
                    f"{wire_keys[wire_key]} vs {_frame_selector(plan)}"
                )
            wire_keys[wire_key] = _frame_selector(plan)

        assert self._serial is not None
        body = {
            "seriennummer": self._serial,
            "keys": sorted(wire_keys),
        }
        status, text = await self._post(
            f"/rest/canip/{_path_segment(self._serial)}/request", body
        )
        if status != 200:
            raise HttpError(
                f"DachsConnect returned HTTP {status} for the datapoint request",
                status=status,
            )
        response = parse_json(text)
        if not isinstance(response, dict):
            raise ProtocolError("The datapoint request response was not an object")
        if response.get("success") is not True:
            message = response.get("message", "no reason given")
            raise ProtocolError(f"Datapoint request rejected: {message}")

        wanted = {key.rpartition(".")[2] for key in wire_keys}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        live: dict[str, list[Mapping[str, Any]]] = {}
        cached: dict[str, list[Mapping[str, Any]]] = {}
        remaining = set(wanted)
        settle_deadline: float | None = None
        while True:
            now = loop.time()
            if remaining:
                if remaining is not None and cached:
                    uncovered = [
                        md5
                        for md5 in remaining
                        if md5 not in cached
                    ]
                    if not uncovered:
                        # Every outstanding md5 already answered from the
                        # portal cache; waiting for live values would
                        # only burn the deadline (offline unit).
                        if settle_deadline is None:
                            settle_deadline = now + 0.3
                            remaining = set()
                        left = settle_deadline - now
                    else:
                        left = deadline - now
                else:
                    left = deadline - now
            else:
                if settle_deadline is None:
                    # All datapoints answered once; give sibling array
                    # element frames a short window to arrive as well.
                    settle_deadline = now + 0.3
                left = settle_deadline - now
            if left <= 0:
                break
            try:
                frame = await asyncio.wait_for(self._next_value_frame(), timeout=left)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            data = frame["data"]
            md5 = data["dataPointName"]
            age = data.get("age")
            if isinstance(age, int) and age > 0:
                # One cached element per md5; the live set wins later.
                if md5 not in cached and md5 not in live:
                    cached[md5] = [data]
            else:
                bucket = live.setdefault(md5, [])
                if data not in bucket:
                    bucket.append(data)
                remaining.discard(md5)
        merged: dict[str, list[Mapping[str, Any]]] = {}
        used_cache = False
        for md5 in wanted:
            if md5 in live:
                merged[md5] = live[md5]
            elif md5 in cached:
                merged[md5] = cached[md5]
                used_cache = True
        return merged, used_cache

    # --- planning ----------------------------------------------------------------

    def _resolve_point(self, code: str, board: str | None) -> tuple[str, MetaDataPoint]:
        """Board + point for a stable code; deterministic or nothing."""
        assert self._catalogue is not None
        candidates = [board] if board is not None else sorted(self._catalogue.board_to_codes)
        hits: list[tuple[str, MetaDataPoint]] = []
        for name in candidates:
            point = self._catalogue.board_to_codes.get(name, {}).get(code)
            if point is not None:
                hits.append((name, point))
        if not hits or (board is None and len(hits) > 1):
            raise KeyError(code)
        return hits[0]

    def _plan_for(self, entry: str) -> _Plan:
        """Parse ``CODE``, ``BOARD.CODE`` or ``CODE[index]`` request names."""
        base = entry
        index: int | None = None
        if "[" in base:
            base, _, suffix = base.partition("[")
            index = int(suffix.rstrip("]"))
        board: str | None
        if "." in base:
            board, _, code = base.partition(".")
        else:
            board, code = None, base
        resolved_board, point = self._resolve_point(code, board)
        return _Plan(
            key=entry,
            code=code,
            board=resolved_board,
            point=point,
            index=index,
        )

    # --- native datapoint access -------------------------------------------------

    async def read_datapoints(self, codes: Sequence[str] | str | None = None) -> dict[str, CanIpValue]:
        """Read CanIP datapoints by stable code — the **native v2 access**.

        ``codes`` entries are the app/manual codes (``"MM000"``), or
        board-scoped (``"CU-CHP01.MM000"``) when one code exists on
        several boards, or an array element (``"BM001[2]"``), or a plain
        array code (``"AM029"`` — answers surface as ``"AM029[0]"``
        etc.). Ambiguity without the board raises ``ValueError`` and
        names the boards.

        ``codes=None`` (the default) reads **every** datapoint of the
        connected unit. That is one request followed by up to a few
        thousand websocket frames — heavy but complete, like the unit's
        own "everything" screen. Returns a dict keyed by the stable
        code (board-scoped only where the code is ambiguous).

        Codes the connected unit does not carry raise :class:`ValueError`.
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._unit is None or self._catalogue is None or self._serial is None:
            await self.connect()
            assert self._unit is not None and self._catalogue is not None
        if codes is None:
            assert self._catalogue is not None
            ambiguous = {
                code
                for code, boards in self._catalogue.code_to_boards.items()
                if len(boards) > 1
            }
            # Plain codes where they are unambiguous, board-scoped where
            # a code exists on several boards.
            codes = [
                f"{board}.{code}" if code in ambiguous else code
                for board, mapping in self._catalogue.board_to_codes.items()
                for code in sorted(mapping)
            ]
        if isinstance(codes, str):
            codes = [codes]
        requested = normalize_requested_keys(list(codes))
        if not requested:
            return {}
        await self._ensure_ws()
        plans: list[_Plan] = []
        for entry in requested:
            try:
                plans.append(self._plan_for(entry))
            except KeyError as error:
                raise ValueError(
                    f"Datapoint not on this unit: {error.args[0]!r}"
                ) from error
        frames, used_cache = await self._collect_frames(plans)
        self._values_stale = used_cache

        values: dict[str, CanIpValue] = {}
        for plan in plans:
            candidates = frames.get(plan.point.id, [])
            if not candidates:
                continue  # no answer for this datapoint — honestly missing
            if plan.point.array and plan.index is None:
                # A plain array code expands into one entry per element.
                for data in candidates:
                    index = data.get("index")
                    if not isinstance(index, int):
                        continue
                    display = f"{plan.key}[{index}]"
                    values[display] = _canip_value_from_frame(
                        plan.board, display, plan.point, data,
                        self._init, self._language,
                    )
                continue
            picked = _pick_frame(candidates, plan.index)
            if picked is None:
                continue
            display = plan.key
            if plan.point.array:
                frame_index = picked.get("index")
                display = (
                    f"{plan.key.split('[', 1)[0]}"
                    f"[{frame_index if isinstance(frame_index, int) else plan.index}]"
                )
            values[display] = _canip_value_from_frame(
                plan.board, display, plan.point, picked, self._init, self._language
            )
        return values

    # --- first-generation facade compatibility ------------------------------------

    async def read_keys(self, keys: Sequence[str]) -> dict[str, str]:
        """Answer first-generation MSR2 GLT keys where evidence allows.

        Only keys whose physical meaning is provably the same for this
        backend are answered (see :data:`COMPAT_BY_KEY` and the catalogue
        module docstring). Values follow the first-generation wire
        semantics (power kW, runtimes in hours) so the shared snapshot
        model reads identically. Everything else stays absent — the
        typed model reports ``MISSING`` rather than guessing.
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._unit is None or self._catalogue is None or self._serial is None:
            await self.connect()
            assert self._unit is not None and self._catalogue is not None
        requested = normalize_requested_keys(list(keys))
        if not requested:
            return {}

        values: dict[str, str] = dict(self._identity_answers(requested))
        compat_plans: list[_Plan] = []
        for key in requested:
            if key in values:
                continue
            spec = COMPAT_BY_KEY.get(key)
            if spec is None or key not in self._compat_ids:
                continue  # honestly unanswered for this backend
            point = self._catalogue.point(spec.code, spec.board)
            compat_plans.append(
                _Plan(key, spec.code, spec.board, point, None)
            )
        if not compat_plans:
            return values

        await self._ensure_ws()
        frames, used_cache = await self._collect_frames(compat_plans)
        self._values_stale = used_cache
        for plan in compat_plans:
            candidates = frames.get(plan.point.id, [])
            data = _pick_frame(candidates, None)
            if data is None or data.get("value") is None:
                continue
            converted = self._compat_wire_text(plan, data["value"])
            if converted is not None:
                values[plan.key] = converted
        return values

    def _compat_wire_text(self, plan: _Plan, raw: Any) -> str | None:
        """Gain + unit conversion for one compatible value."""
        if isinstance(raw, bool) or isinstance(raw, str) or not isinstance(
            raw, (int, float, Decimal)
        ):
            return None
        spec = COMPAT_BY_KEY[plan.key]
        gain = _gain_decimal(plan.point.gain)
        try:
            applied = (
                apply_gain(raw, plan.point.gain)
                if gain is not None
                else Decimal(str(raw))
            )
        except (InvalidOperation, TypeError):
            return None
        if spec.convert is not None:
            return spec.convert(applied)
        return to_wire_text(applied)

    def _identity_answers(self, requested: Sequence[str]) -> dict[str, str]:
        """Identity values synthesized from the unit payload (no frames)."""
        if self._unit is None:
            return {}
        unit = self._unit
        candidates: dict[str, str] = {}
        serial = unit.get("seriennummer")
        if isinstance(serial, str):
            candidates["Hka_Bd_Stat.uchSeriennummer"] = serial
        part = unit.get("artikelNummer")
        if isinstance(part, str):
            candidates["Hka_Bd_Stat.uchTeilenummer"] = part
        commissioned = unit.get("inbetriebnahmeDatum")
        if isinstance(commissioned, int) and not isinstance(commissioned, bool):
            seconds = Decimal(commissioned) / 1000 - _EPOCH_2000_SECONDS
            if seconds == seconds.to_integral_value():
                candidates["Hka_Bd_Stat.ulInbetriebnahmedatum"] = str(int(seconds))
        return {key: value for key, value in candidates.items() if key in set(requested)}

    # --- status, errors, charts, writes ------------------------------------------

    async def get_errors(self, only_current: bool = True) -> list[CanIpError]:
        """Error history of the connected unit's boards (payload snapshot).

        ``only_current=True`` returns only errors active at reading time.
        Freshness option: errors refresh on reconnect or with
        ``fetchErrors``; this method answers from the last connect.
        """
        if self._unit is None or self._init is None:
            raise RuntimeError("No unit connected — call connect() first")
        categories = _error_categories(self._init, self._language)
        out: list[CanIpError] = []
        for board in self._unit.get("boards", []):
            if not isinstance(board, Mapping):
                continue
            for entry in board.get("canipErrors") or []:
                if not isinstance(entry, Mapping):
                    continue
                if only_current and entry.get("currentError") is not True:
                    continue
                out.append(_canip_error(board, entry, categories))
        return out

    async def chart_names(self) -> list[str]:
        """Chart names the connected unit offers (per-unit catalog)."""
        if self._unit is None:
            raise RuntimeError("No unit connected — call connect() first")
        charts = self._unit.get("charts")
        if not isinstance(charts, Mapping):
            return []
        catalog = charts.get("charts")
        if isinstance(catalog, Mapping):
            return sorted(str(name) for name in catalog)
        return []

    async def chart(
        self,
        name: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Fetch one history chart (raw JSON answer, per-unit catalog).

        ``start``/``end`` accept ISO strings (``"2026-11-08T00:00:00Z"``)
        or datetimes; defaults cover the last 24 hours. Response shapes
        carry ``from``/``to`` in epoch milliseconds, ``boardMap`` rows
        per board in epoch seconds and a ``charts`` echo.
        """
        if self._serial is None:
            raise RuntimeError("No unit connected — call connect() first")
        start_text = _iso_time(start, "start") or (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_text = _iso_time(end, "end") or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        payload = {
            "start": start_text,
            "end": end_text,
            "parameters": {},
            "chartName": name,
            "sn": self._serial,
        }
        status, text = await self._post(
            f"/rest/charts/{_path_segment(self._serial)}/data", payload
        )
        if status != 200:
            raise HttpError(
                f"DachsConnect returned HTTP {status} for chart {name!r}",
                status=status,
            )
        body = parse_json(text)
        if not isinstance(body, dict):
            raise ProtocolError(f"The chart {name!r} response was not an object")
        return body

    async def write_datapoint(
        self, code: str, value: Any, *, board: str | None = None
    ) -> dict[str, Any]:
        """Write one CanIP datapoint (EXPERIMENTAL).

        The endpoint is documented by the production frontend, but no
        write against a real account has verified it end-to-end: treat
        the first use as an experiment. ``value`` is the **raw** pre-gain
        wire value (e.g. ``1`` when the metadata gain is 0.1). Errors
        are raised with ``outcome_unknown=True`` and are never retried —
        this is a physical heating controller.
        """
        if self._unit is None or self._serial is None:
            raise RuntimeError("No unit connected — call connect() first")
        resolved_board, point = self._resolve_point(code, board)
        payload = {"boardName": resolved_board, "values": {point.id: value}}
        status, text = await self._post(
            f"/rest/canip/{_path_segment(self._serial)}/write", payload
        )
        if status != 200:
            raise WriteError(
                f"DachsConnect returned HTTP {status} writing datapoint "
                f"{_mask_code(resolved_board, code)}; whether the write "
                f"reached the unit is unknown"
            )
        try:
            body = parse_json(text)
        except ProtocolError:
            body = {"raw": text}
        if not isinstance(body, dict):
            return {"raw": text}
        return body

    async def request_run(self) -> None:
        """Not available on DachsConnect — the call raises always.

        The cloud v2 backend has no one-shot run request equivalent to
        the older portal's ``dachsAnfordern`` (there is no such endpoint
        or SPA action).
        """
        raise WriteError(
            "DachsConnect (cloud v2) has no one-shot run request; this "
            "backend cannot issue one. Nothing was sent (outcome known: "
            "no write happened).",
            outcome_unknown=False,
        )

    async def request_power(self, modules: int) -> None:
        """Not available on DachsConnect — the call raises always.

        Power-leaving datapoints exist (``NM020``/``NM024``/``NP190``
        family) but their write semantics are unverified against a live
        unit; the demanded-level write path stays an experiment behind
        :meth:`write_datapoint` rather than a pretend facade method.
        """
        raise WriteError(
            "DachsConnect (cloud v2) has no verified external "
            "power-demand write; nothing was sent (outcome known: no "
            "write happened).",
            outcome_unknown=False,
        )


def _error_categories(
    init: Mapping[str, Any], language: str
) -> dict[tuple[int, int], tuple[str | None, str | None]]:
    """Category/code → (category translation, code translation)."""
    translations = init.get("translations")
    if not isinstance(translations, Mapping):
        return {}
    block = translations.get(language)
    if not isinstance(block, Mapping):
        return {}
    categories = block.get("errorCategories")
    if not isinstance(categories, Mapping):
        return {}
    result: dict[tuple[int, int], tuple[str | None, str | None]] = {}
    for key, entry in categories.items():
        try:
            category_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, Mapping):
            continue
        texts = entry.get("translations")
        category_text = entry.get("translationMedium")
        if isinstance(texts, Mapping):
            for code_key, text in texts.items():
                try:
                    code_id = int(code_key)
                except (TypeError, ValueError):
                    continue
                result[(category_id, code_id)] = (
                    category_text if isinstance(category_text, str) else None,
                    text if isinstance(text, str) else None,
                )
    return result


def _canip_error(
    board: Mapping[str, Any],
    entry: Mapping[str, Any],
    categories: Mapping[tuple[int, int], tuple[str | None, str | None]],
) -> CanIpError:
    error_code = entry.get("errorCode")
    category_id = (
        error_code.get("category")
        if isinstance(error_code, Mapping) and isinstance(error_code.get("category"), int)
        else None
    )
    code_id = (
        error_code.get("code")
        if isinstance(error_code, Mapping) and isinstance(error_code.get("code"), int)
        else None
    )
    category_text, translation = (
        categories.get((category_id, code_id), (None, None))
        if category_id is not None and code_id is not None
        else (None, None)
    )
    timestamp = entry.get("timestamp")
    counter = entry.get("counter")
    name = (
        entry.get("boardName")
        if isinstance(entry.get("boardName"), str) and entry.get("boardName")
        else board.get("name") if isinstance(board.get("name"), str) else ""
    )
    code_text = entry.get("codeText") if isinstance(entry.get("codeText"), str) else None
    return CanIpError(
        board=name or "",
        code_text=code_text or "",
        category=category_id if category_id is not None else -1,
        code=code_id if code_id is not None else -1,
        current=entry.get("currentError") is True,
        valid=entry.get("valid") is True,
        timestamp_ms=timestamp if isinstance(timestamp, int) else None,
        counter=counter if isinstance(counter, int) else None,
        category_text=category_text,
        translation=translation,
    )


def _path_segment(serial: str) -> str:
    return quote(str(serial), safe="")


def _frame_selector(plan: _Plan) -> str:
    """Uniqueness tag of one plan inside the same md5 (array elements)."""
    return "" if plan.index is None else f"[{plan.index}]"


def _pick_frame(
    candidates: list[Mapping[str, Any]], index: int | None
) -> Mapping[str, Any] | None:
    """Pick the best frame: matching array element, live before cached."""
    fallback: Mapping[str, Any] | None = None
    for data in candidates:
        if index is not None and data.get("index") != index:
            fallback = fallback or (
                data if isinstance(data.get("index"), int) else fallback
            )
            continue
        if index is None and isinstance(data.get("index"), int):
            # Scalar datapoint receiving an element frame — skip it.
            continue
        fallback = data
        break
    return fallback


def _mask_code(board: str, code: str) -> str:
    return f"{board}.{code}" if board else code


def _iso_time(value: datetime | str | None, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        target = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{field} must be a datetime or ISO string")
