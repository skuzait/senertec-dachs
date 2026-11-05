"""Wire protocol helpers for the MSR2 local GLT interface.

Parsing and encoding only; no HTTP or session handling lives here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from yarl import URL

from ..errors import ProtocolError, WriteError

POWER_ACTIVATION_KEY = "Stromf_Ew.Anforderung_GLT.bAktiv"
POWER_MODULE_COUNT_KEY = "Stromf_Ew.Anforderung_GLT.bAnzahlModule"
MIN_POWER_MODULES = 0
MAX_POWER_MODULES = 10


def parse_read_response(text: str, requested: Sequence[str]) -> dict[str, str]:
    """Parse a ``GET /getKey`` response body into a key/value mapping.

    Blank lines are ignored; each remaining line is split at its first
    ``=`` with surrounding whitespace trimmed. Malformed lines, duplicate
    keys, or keys that were not requested raise :class:`ProtocolError`
    instead of being silently overwritten. An empty value (``key=``) is a
    valid transport result and surfaces as ``""``.
    """
    requested_keys = set(requested)
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ProtocolError(f"Malformed response line without '=' separator: {line!r}")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ProtocolError(f"Malformed response line with empty key: {line!r}")
        if key not in requested_keys:
            raise ProtocolError(f"Response contains unrequested key: {key}")
        if key in values:
            raise ProtocolError(f"Response contains duplicate key: {key}")
        values[key] = value
    return values


def chunk_read_requests(
    keys: Sequence[str], base_url: URL, max_url_length: int
) -> list[list[str]]:
    """Split keys into sequential GET chunks respecting the URL length limit.

    The limit applies to the complete percent-encoded URL including host,
    port, and path. Chunks are built greedily in request order; a single
    key that cannot fit raises :class:`ValueError` before any I/O.
    """
    if max_url_length <= 0:
        raise ValueError("max_url_length must be positive")
    chunks: list[list[str]] = []
    current: list[str] = []
    for key in keys:
        candidate = [*current, key]
        url = base_url.with_query([("k", item) for item in candidate])
        if len(str(url)) <= max_url_length:
            current = candidate
            continue
        if not current:
            raise ValueError(
                f"Single key exceeds the configured URL length limit: {key}"
            )
        chunks.append(current)
        current = [key]
    if current:
        chunks.append(current)
    return chunks


def encode_power_request(modules: int) -> list[tuple[str, str]]:
    """Encode the documented external power-demand write fields.

    The activation flag must accompany the module count in the same
    request; the controller resets the flag after adopting the value
    (SenerTec 11/4798.444.000, p. 10).
    """
    return [
        (POWER_ACTIVATION_KEY, "1"),
        (POWER_MODULE_COUNT_KEY, str(modules)),
    ]


def parse_write_acknowledgement(text: str, expected: Sequence[str]) -> Mapping[str, str]:
    """Parse a ``POST /setKeys`` acknowledgement body.

    Success requires exactly one ``<key> ok`` line for every expected key,
    in any order. Anything else (missing, duplicate, unexpected, malformed,
    or non-ok status) raises :class:`WriteError` with ``outcome_unknown``
    set, because the controller may already have applied part of the write.
    """
    expected_keys = set(expected)
    acknowledgements: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, status = line.partition(" ")
        key = key.strip()
        status = status.strip()
        if not separator or not key or not status:
            raise WriteError(
                f"Malformed acknowledgement line: {line!r}",
                acknowledgements=acknowledgements,
            )
        if key not in expected_keys:
            raise WriteError(
                f"Unexpected acknowledgement key: {key}",
                acknowledgements=acknowledgements,
            )
        if key in acknowledgements:
            raise WriteError(
                f"Duplicate acknowledgement for key: {key}",
                acknowledgements=acknowledgements,
            )
        acknowledgements[key] = status
    missing = expected_keys - acknowledgements.keys()
    if missing:
        raise WriteError(
            f"Missing acknowledgement for key(s): {', '.join(sorted(missing))}",
            acknowledgements=acknowledgements,
        )
    rejected = {key: status for key, status in acknowledgements.items() if status != "ok"}
    if rejected:
        raise WriteError(
            "Write not acknowledged: " + ", ".join(f"{key} -> {status}" for key, status in sorted(rejected.items())),
            acknowledgements=acknowledgements,
        )
    return acknowledgements
