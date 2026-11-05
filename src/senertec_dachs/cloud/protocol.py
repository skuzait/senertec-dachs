"""Wire protocol helpers for the Dachsportal (cloud) API.

The portal is the backend of the official DachsApp: form-POST requests
with a ``token`` field plus the session cookies set by ``checkLogin``,
gzip-compressed JSON responses. Keys carry a ``@<module>`` suffix (module
``L1`` answers as ``@1``). JSON numbers are parsed with
:class:`~decimal.Decimal` so no precision is lost when values are mapped
onto the shared wire-text format of the typed model.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from ..errors import ProtocolError

BASE_URL = "https://dachsportal.senertec.com/dachsportal-app2/msrData"
#: Client identification sent with every login, as observed in the app.
APP_REVISION = "v2.0.0-265"
API_REVISION = "666"

_MODULE_SUFFIX = re.compile(r"@\d+$")


def to_cloud_key(local_key: str, module: str) -> str:
    """Append the portal module suffix (e.g. ``Hka_Bd.ulAnzahlStarts@1``)."""
    return f"{local_key}@{module}"


def to_local_key(cloud_key: str) -> str:
    """Strip a trailing ``@<module>`` suffix from a portal key."""
    return _MODULE_SUFFIX.sub("", cloud_key)


def encode_read_request(cloud_keys: list[str]) -> str:
    """Encode the ``keys`` form field for ``get`` (unformatted raw values)."""
    return json.dumps([{"key": key, "format": ""} for key in cloud_keys])


def parse_json(text: str) -> Any:
    """Parse a portal JSON body with decimals kept exact (no float rounding)."""
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError as error:
        raise ProtocolError(
            f"The Dachsportal returned a non-JSON response: {text[:120]!r}"
        ) from error


def wants_relogin(body: Any) -> bool:
    """Whether a response body reports an expired session."""
    return isinstance(body, dict) and body.get("relogin") is True


def to_wire_text(value: Any) -> str:
    """Convert a portal JSON value to the shared wire-text format.

    Booleans map to ``true``/``false`` (the local wire format), numbers
    to their exact decimal text, strings unchanged. The
    result feeds straight into the shared typed model.
    """
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ProtocolError(
        f"The Dachsportal returned an unsupported value type: {value!r}"
    )
