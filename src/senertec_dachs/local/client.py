"""Async client for the Dachs MSR2 local GLT interface (HTTP port 8080)."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from math import isfinite

import aiohttp
from yarl import URL

from .._reader import SnapshotReaderMixin, normalize_requested_keys
from ..errors import (
    AuthenticationError,
    ConnectionError,
    DachsError,
    HttpError,
    RequestTimeoutError,
)
from . import protocol
from .protocol import MAX_POWER_MODULES, MIN_POWER_MODULES

DEFAULT_PORT = 8080
DEFAULT_USERNAME = "glt"
DEFAULT_TIMEOUT = 10.0
#: Conservative default; the device URL limit is documented but not
#: quantified by SenerTec.
DEFAULT_MAX_URL_LENGTH = 1800


class LocalClient(SnapshotReaderMixin):
    """Asynchronous client for one Dachs MSR2 Ethernet interface.

    The constructor performs no I/O. Pass an ``aiohttp.ClientSession`` to
    share a connection pool; it stays owned by the caller. Otherwise a
    session is created lazily and closed by :meth:`close` / ``async with``.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        username: str = DEFAULT_USERNAME,
        password: str = "",
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_url_length: int = DEFAULT_MAX_URL_LENGTH,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty IP address or DNS name")
        if any(character.isspace() for character in host):
            raise ValueError("host must not contain whitespace")
        if "://" in host or any(marker in host for marker in "/?#@"):
            raise ValueError(
                "host must be a bare IP address or DNS name, not a URL"
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number of seconds")
        if (
            isinstance(max_url_length, bool)
            or not isinstance(max_url_length, int)
            or max_url_length <= 0
        ):
            raise ValueError("max_url_length must be a positive integer")

        # yarl expects bare (unbracketed) IPv6 hosts and brackets them itself.
        url_host = (
            host[1:-1]
            if host.startswith("[") and host.endswith("]")
            else host
        )
        credentials = f"{username}:{password}".encode("latin-1")
        self._auth_headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii")
        }
        self._get_url = URL.build(scheme="http", host=url_host, port=port, path="/getKey")
        self._set_url = URL.build(scheme="http", host=url_host, port=port, path="/setKeys")
        self._request_timeout = aiohttp.ClientTimeout(total=timeout)
        self._timeout_seconds = float(timeout)
        self._max_url_length = max_url_length
        self._session = session
        self._owns_session = session is None
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether this client has been closed and can no longer be used."""
        return self._closed

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the client. Idempotent; only owned sessions are closed."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> LocalClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _request(
        self, method: str, url: URL, data: list[tuple[str, str]] | None = None
    ) -> str:
        session = self._ensure_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._auth_headers,
                timeout=self._request_timeout,
                allow_redirects=False,
                data=data,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError(
                        f"Device rejected the credentials (HTTP {response.status})"
                    )
                if not 200 <= response.status < 300:
                    raise HttpError(
                        f"Unexpected HTTP status {response.status} from device",
                        status=response.status,
                    )
                return await response.text()
        except DachsError:
            raise
        except TimeoutError as error:
            raise RequestTimeoutError(
                f"Device did not answer within {self._timeout_seconds} seconds"
            ) from error
        except aiohttp.ClientConnectionError as error:
            raise ConnectionError(f"Could not reach the device: {error}") from error
        except aiohttp.ClientError as error:
            raise ConnectionError(f"Request to the device failed: {error}") from error

    async def read_keys(self, keys: Sequence[str]) -> dict[str, str]:
        """Read raw wire values for the given keys.

        Works with any non-empty key, including undocumented ones. Keys are
        deduplicated preserving first-seen order; an empty sequence returns
        ``{}`` without any HTTP call. Long key lists are split into
        sequential requests whose complete percent-encoded URLs respect
        ``max_url_length``. Absent keys stay absent from the result.
        """
        requested = normalize_requested_keys(keys)
        if not requested:
            return {}
        values: dict[str, str] = {}
        for chunk in protocol.chunk_read_requests(
            requested, self._get_url, self._max_url_length
        ):
            url = self._get_url.with_query([("k", key) for key in chunk])
            text = await self._request("GET", url)
            values.update(protocol.parse_read_response(text, chunk))
        return values


    async def request_power(self, modules: int) -> None:
        """Request external power demand for ``modules`` modules (0-10).

        Sends exactly one form-encoded ``POST /setKeys`` setting the
        activation flag together with the module count. The demand stays
        active for ten minutes; sustained demand must be renewed by the
        caller at a shorter interval (SenerTec 11/4798.444.000, p. 10).
        A clean acknowledgement proves acceptance by the controller, not
        that a module actually started. Uncertain outcomes raise with
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
        fields = protocol.encode_power_request(modules)
        expected_keys = [key for key, _ in fields]
        try:
            text = await self._request("POST", self._set_url, data=fields)
        except DachsError as error:
            error.outcome_unknown = True
            raise
        protocol.parse_write_acknowledgement(text, expected_keys)

    async def request_run(self) -> None:
        """Issue a one-shot run request (documented GLT demand, one module).

        The local interface has no dedicated run endpoint; its canonical
        one-shot demand is the documented external-demand write with a
        module count of 1 — the same intent as the app's cloud-side run
        request. See :meth:`request_power` for the demand semantics.
        """
        await self.request_power(1)
