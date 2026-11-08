"""Async client for the SenerTec Dachsportal (cloud) API.

The portal is the backend the official DachsApp talks to. It needs a
portal ID — the trailing part of the Dachs serial number — and its
password; no local network access is required. A session combines a
token (sent as a form field) with the HTTP cookies set by
``checkLogin``; both are managed automatically, and one transparent
re-login is attempted when the portal reports an expired session
(HTTP 401 with ``{"relogin": true}``). A failed login invalidates the
current session server-side, so failed logins are never retried blindly.
Login and session check are the same ``checkLogin`` call, matching the
official app.

All errors carry human-friendly messages: rejected credentials, an
unreachable or slow portal, and expired sessions are described in plain
language rather than raw HTTP/stack vocabulary.

Freshness: the portal polls the device every few minutes and serves the
last successful poll, so values can be slightly stale or — when the
device is unreachable for the portal — outdated. :meth:`read_snapshot`
therefore calls ``metainfoLong`` first and records the portal's own
``offline`` flag and ``fetchedAt`` timestamp (see :attr:`last_status` and
:class:`CloudStatus`); a snapshot never claims to be fresher than the
portal says it is. Values are also aggregates, so expect small rounding
differences versus the local interface, and note that the portal's
current ``Hka_Bd.ulArbeitThermHka`` value includes the condenser share
while the local key reports the unit alone.

Writes: :meth:`request_run` issues the one-shot run request
(``dachsAnfordern`` — the app's manual "Anfrage" button; no module
count, the portal applies its own default demand). :meth:`request_power`
sends the documented external-demand keys through the portal ``set``
endpoint; that GLT-key path is unconfirmed against a real portal —
treat its first real use as an experiment. Portal ``set`` values are
strings, booleans included (``"true"``/``"false"``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from typing import Any

import aiohttp
from yarl import URL

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
from ..keys import LOCAL_KEYS
from ..local.protocol import (
    MAX_POWER_MODULES,
    MIN_POWER_MODULES,
    POWER_ACTIVATION_KEY,
    POWER_MODULE_COUNT_KEY,
)
from ..models import Snapshot
from . import protocol
from .protocol import BASE_URL

DEFAULT_TIMEOUT = 30.0
#: The portal's module index for a single-module plant (``L1`` -> ``@1``).
DEFAULT_MODULE = "1"


@dataclass(frozen=True, slots=True)
class CloudStatus:
    """What the portal says about the device behind the account."""

    device_online: bool | None
    last_fetched: datetime | None
    modules: tuple[str, ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_metainfo(cls, metainfo: Mapping[str, Any]) -> CloudStatus:
        offline = metainfo.get("offline")
        fetched_at: datetime | None = None
        fetched_ms = metainfo.get("fetchedAt")
        if isinstance(fetched_ms, (int, Decimal)) and not isinstance(fetched_ms, bool):
            milliseconds = int(fetched_ms)
            fetched_at = datetime.fromtimestamp(
                milliseconds // 1000, tz=timezone.utc
            ).replace(microsecond=(milliseconds % 1000) * 1000)
        modules = metainfo.get("module")
        return cls(
            device_online=(not offline) if isinstance(offline, bool) else None,
            last_fetched=fetched_at,
            modules=tuple(modules) if isinstance(modules, list) else (),
            raw=metainfo,
        )


class CloudClient(SnapshotReaderMixin):
    """Asynchronous client for one Dachs account on the Dachsportal.

    The constructor performs no I/O; the login happens lazily on the
    first request and is repeated transparently (once) when the portal
    reports an expired session. Pass your own ``aiohttp.ClientSession``
    to share a connection pool — it must keep cookies (the default
    cookie jar does; ``DummyCookieJar`` breaks portal sessions).
    """

    def __init__(
        self,
        portal_id: str,
        password: str,
        *,
        base_url: str = BASE_URL,
        module: str = DEFAULT_MODULE,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not isinstance(portal_id, str) or not portal_id.strip():
            raise ValueError(
                "portal_id must be the non-empty trailing part of the Dachs "
                "serial number (e.g. '12345')"
            )
        if any(character.isspace() for character in portal_id):
            raise ValueError("portal_id must not contain whitespace")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if not isinstance(module, str) or not module or "@" in module:
            raise ValueError("module must be a non-empty module index like '1'")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number of seconds")
        self._portal_id = portal_id
        self._password = password
        self._base = URL(base_url)
        self._module = module
        self._request_timeout = aiohttp.ClientTimeout(total=timeout)
        self._timeout_seconds = float(timeout)
        self._session = session
        self._owns_session = session is None
        self._token: str | None = None
        self._last_status: CloudStatus | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_status(self) -> CloudStatus | None:
        """The portal status recorded by the last :meth:`read_snapshot`."""
        return self._last_status

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._session is None:
            # The portal session lives in its cookies as much as in the
            # token. Keep them for any base_url, including IP-based ones
            # (aiohttp's default jar drops cookies received from IP hosts).
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._session

    async def close(self) -> None:
        """Close the client. Idempotent; only owned sessions are closed."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None
        self._token = None

    async def __aenter__(self) -> CloudClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _post(self, endpoint: str, data: Mapping[str, str]) -> tuple[int, Any]:
        """POST form data and return ``(http_status, parsed_json)``."""
        session = self._ensure_session()
        url = self._base / endpoint
        try:
            async with session.post(
                url,
                data=dict(data),
                timeout=self._request_timeout,
                allow_redirects=False,
            ) as response:
                status = response.status
                text = await response.text()
        except DachsError:
            raise
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"The Dachsportal did not answer within "
                f"{self._timeout_seconds} seconds — the portal may be down "
                f"or busy; try again later"
            ) from error
        except aiohttp.ClientConnectionError as error:
            raise ConnectionError(
                f"Could not reach the SenerTec Dachsportal: {error}"
            ) from error
        except aiohttp.ClientError as error:
            raise ConnectionError(
                f"Request to the SenerTec Dachsportal failed: {error}"
            ) from error
        if not 200 <= status < 300:
            try:
                parsed = protocol.parse_json(text)
            except ProtocolError:
                parsed = None
            return status, parsed
        return status, protocol.parse_json(text)

    async def _login(self) -> None:
        """Log in and store the token (the session cookies come along)."""
        status, body = await self._post(
            "checkLogin",
            {
                "revision": protocol.APP_REVISION,
                "sn": self._portal_id,
                "passwd": self._password,
                "apiRevision": protocol.API_REVISION,
            },
        )
        if not isinstance(body, dict) or body.get("loginOk") is not True:
            reason = (
                body.get("message", "no reason given")
                if isinstance(body, dict)
                else f"HTTP {status}"
            )
            raise AuthenticationError(
                f"The Dachsportal rejected the portal ID or password "
                f"({reason}). The portal ID is the trailing part of the "
                f"Dachs serial number, e.g. '12345'."
            )
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolError(
                "The Dachsportal login response did not contain a token."
            )
        self._token = token

    async def _ensure_token(self) -> str:
        if self._token is None:
            await self._login()
        assert self._token is not None
        return self._token

    async def _request(self, endpoint: str, data: Mapping[str, str]) -> Any:
        """POST with the session token; re-login once if it expired."""
        payload: dict[str, str] = dict(data)
        for attempt in range(2):
            payload["token"] = await self._ensure_token()
            status, body = await self._post(endpoint, payload)
            if status == 401 or protocol.wants_relogin(body):
                # A failed login invalidates the session server-side, so
                # only ever re-login once and let errors surface.
                self._token = None
                if attempt == 0:
                    continue
                raise AuthenticationError(
                    "The Dachsportal session expired and re-logging in did "
                    "not restore it. Try again in a moment; if it persists, "
                    "check the portal ID and password or whether the app is "
                    "logged in elsewhere."
                )
            if not 200 <= status < 300:
                reason = (
                    body.get("message")
                    if isinstance(body, dict)
                    else None
                )
                suffix = f" ({reason})" if reason else ""
                raise HttpError(
                    f"The Dachsportal returned HTTP {status} for "
                    f"{endpoint}{suffix}",
                    status=status,
                )
            return body
        raise AssertionError("unreachable")

    # --- shared reading API -------------------------------------------------

    async def read_keys(self, keys: Sequence[str]) -> dict[str, str]:
        """Read raw values for the given keys from the portal.

        Keys are deduplicated preserving first-seen order; an empty
        sequence returns ``{}`` without any HTTP call. Values are the
        portal's cached device data in the shared wire-text format; a
        ``null`` value leaves the key absent (``MISSING`` in the typed
        model). Unknown keys are answered with an empty string by the
        portal and surface as ``INVALID``.
        """
        requested = normalize_requested_keys(keys)
        if not requested:
            return {}
        cloud_keys = [protocol.to_cloud_key(key, self._module) for key in requested]
        body = await self._request(
            "get", {"keys": protocol.encode_read_request(cloud_keys)}
        )
        if not isinstance(body, dict):
            raise ProtocolError(
                "The Dachsportal returned an unexpected response for get: "
                f"{type(body).__name__}"
            )
        requested_set = set(requested)
        values: dict[str, str] = {}
        for cloud_key, value in body.items():
            if cloud_key.startswith("app."):
                continue  # portal status block, not device data
            key = protocol.to_local_key(cloud_key)
            if key not in requested_set:
                raise ProtocolError(
                    f"The Dachsportal returned unrequested key: {cloud_key}"
                )
            if value is None:
                continue
            values[key] = protocol.to_wire_text(value)
        return values

    async def read_snapshot(self) -> Snapshot:
        """Read all documented keys plus the portal's freshness status.

        Calls ``metainfoLong`` first (device online? when was it last
        fetched?) and then ``get``; the status is available as
        :attr:`last_status`. Values may be several minutes old — that is
        inherent to the portal, which polls the device periodically.
        """
        metainfo = await self._request("metainfoLong", {})
        if not isinstance(metainfo, dict):
            raise ProtocolError(
                "The Dachsportal returned an unexpected response for "
                "metainfoLong"
            )
        self._last_status = CloudStatus.from_metainfo(metainfo)
        raw = await self.read_keys(list(LOCAL_KEYS))

        return Snapshot.from_raw(raw)

    async def request_power(self, modules: int) -> None:
        """Request external power demand for ``modules`` modules (0-10).

        Sends the documented ``Stromf_Ew.Anforderung_GLT`` fields through
        the portal ``set`` endpoint. Implemented per the portal's write
        format but not yet confirmed against a real portal — treat its
        first real use as an experiment. Uncertain outcomes raise with
        ``outcome_unknown=True`` and are never retried automatically.
        """
        if (
            isinstance(modules, bool)
            or not isinstance(modules, int)
            or not MIN_POWER_MODULES <= modules <= MAX_POWER_MODULES
        ):
            raise ValueError(
                f"modules must be an integer between "
                f"{MIN_POWER_MODULES} and {MAX_POWER_MODULES}"
            )
        fields = {
            protocol.to_cloud_key(POWER_ACTIVATION_KEY, self._module): "1",
            protocol.to_cloud_key(POWER_MODULE_COUNT_KEY, self._module): str(modules),
        }
        try:
            body = await self._request("set", {"keys": json.dumps(fields)})
        except DachsError as error:
            error.outcome_unknown = True
            raise
        if not isinstance(body, dict) or body.get("ok") is not True:
            reason = (
                body.get("message", "no reason given")
                if isinstance(body, dict)
                else "unexpected response shape"
            )
            raise WriteError(
                f"The Dachsportal did not confirm the power-demand change "
                f"({reason})",
                acknowledgements=fields,
            )

    async def request_run(self) -> None:
        """Issue the app's one-shot run request (``dachsAnfordern``).

        The endpoint takes only the session token and answers
        ``{"ok": true, "message": "Laufanforderung abgesetzt!"}``. There
        is no module count — the portal applies its own default demand.
        Uncertain outcomes raise with ``outcome_unknown=True`` and are
        never retried automatically.
        """
        try:
            body = await self._request("dachsAnfordern", {})
        except DachsError as error:
            error.outcome_unknown = True
            raise
        if not isinstance(body, dict) or body.get("ok") is not True:
            reason = (
                body.get("message", "no reason given")
                if isinstance(body, dict)
                else "unexpected response shape"
            )
            raise WriteError(
                f"The Dachsportal did not confirm the run request ({reason})"
            )

    # --- portal-specific extras ----------------------------------------------
    async def status(self) -> CloudStatus:
        """Fetch the portal's view of the device (online state, freshness)."""
        metainfo = await self._request("metainfoLong", {})
        if not isinstance(metainfo, dict):
            raise ProtocolError(
                "The Dachsportal returned an unexpected response for "
                "metainfoLong"
            )
        self._last_status = CloudStatus.from_metainfo(metainfo)
        return self._last_status

    async def metainfo(self) -> dict[str, Any]:
        """Raw ``metainfoLong`` payload (product, modules, settings, …).

        The portal echoes the live session ``token`` inside this payload —
        treat the result as a secret (do not log it verbatim).
        """
        body = await self._request("metainfoLong", {})
        if not isinstance(body, dict):
            raise ProtocolError("The Dachsportal metainfo response was not an object")
        return body

    async def portal_news(self) -> list[Any]:
        """Portal-wide news messages (``nachrichten``), newest first."""
        body = await self._request("nachrichten", {})
        if not isinstance(body, list):
            raise ProtocolError("The Dachsportal news response was not a list")
        return body

    async def reports(self) -> list[Any]:
        """Device reports (``reporting``): maintenance and service messages.

        Note: these messages can contain personal data (operator name,
        address, phone) as stored in the portal.
        """
        body = await self._request("reporting", {})
        if not isinstance(body, list):
            raise ProtocolError("The Dachsportal reports response was not a list")
        return body

    async def daily_profile(self) -> dict[str, Any]:
        """Today's 15-minute run grid (``tageslauf``): ``soll`` vs ``dachs``."""
        body = await self._request("tageslauf", {})
        if not isinstance(body, dict):
            raise ProtocolError("The Dachsportal daily profile response was not an object")
        return body

    async def schedules(self) -> dict[str, Any]:
        """Configured switching times (``schaltzeiten``) as minute ranges."""
        body = await self._request("schaltzeiten", {"type": "0"})
        if not isinstance(body, dict):
            raise ProtocolError("The Dachsportal schedules response was not an object")
        return body

    async def statistics(
        self, von: str, bis: str, *, nummer: str = "0"
    ) -> dict[str, Any]:
        """Monthly statistics (``statistik``) from ``von`` to ``bis``.

        Months are inclusive ``YYYY-M`` strings, e.g. ``von="2026-9"`,
        ``bis="2026-11"``.
        """
        if not isinstance(von, str) or not von.strip():
            raise ValueError("von must be a non-empty month string like '2026-9'")
        if not isinstance(bis, str) or not bis.strip():
            raise ValueError("bis must be a non-empty month string like '2026-11'")
        body = await self._request(
            "statistik", {"nummer": nummer, "von": von, "bis": bis}
        )
        if not isinstance(body, dict):
            raise ProtocolError("The Dachsportal statistics response was not an object")
        return body
