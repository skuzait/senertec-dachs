"""Wire protocol helpers for DachsConnect (DachsPortal2, cloud v2).

DachsConnect serves the latest-generation Dachs (MSR3 controller,
product group ``dachsII`` plus Remeha units on the same platform). It
differs fundamentally from the first-generation portal: authentication
is SAML2 SSO with email and password, unit metadata comes from
``/rest/info/init`` and ``/rest/canip/{serial}`` (CanIP datapoints with
server-side gains, units and enumeration translations), and values
arrive as websocket frames after a ``/rest/canip/{serial}/request``
call.

CanIP datapoints are addressed as ``{board}.{session-scoped md5 id}``;
the human codes seen in the app and the operating manual (``MM000``,
``MC002``, ...) are the ``friendlyName`` of each metadata entry. The md5
ids rotate per session, so this module never hardcodes them — the
catalogue is resolved at runtime from ``init`` plus the unit payload.

JSON numbers are parsed with :class:`~decimal.Decimal` so no precision
is lost when values are mapped onto the shared wire-text format of the
typed model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import ProtocolError

BASE_URL = "https://dachsconnect.senertec.com"
SSO_URL = "https://sso-portal.senertec.com"
#: The websocket endpoint; session cookies authenticate the upgrade.
WS_URL = "wss://dachsconnect.senertec.com/ws"

SUPPORTED_PRODUCT_GROUPS = ("dachsII",)


def parse_json(text: str) -> Any:
    """Parse a DachsConnect JSON body with decimals kept exact."""
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError as error:
        raise ProtocolError(
            f"DachsConnect returned a non-JSON response: {text[:120]!r}"
        ) from error


def to_wire_text(value: Any) -> str:
    """Convert a DachsConnect JSON value to shared wire-text format.

    Booleans map to ``true``/``false`` (the local wire format), numbers
    to their exact decimal text, strings unchanged.
    """
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, str):
        return value
    raise ProtocolError(f"DachsConnect returned an unsupported value type: {value!r}")


def apply_gain(value: Any, gain: int | float | Decimal) -> Decimal:
    """Apply the server-provided gain exactly, with no float arithmetic.

    Metadata gains observed so far are 0.001, 0.01, 0.1, 1 and 10, all
    representable exactly as Decimals.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProtocolError(
            f"CanIP value is not numeric and cannot take a gain: {value!r}"
        )
    exact = value if isinstance(value, Decimal) else Decimal(str(value))
    factor = gain if isinstance(gain, Decimal) else Decimal(str(gain))
    return exact * factor


@dataclass(frozen=True, slots=True)
class MetaDataPoint:
    """One entry of the ``init`` datapoint metadata catalogue."""

    id: str
    code: str | None
    gain: int | float | Decimal
    unit: str | None
    obd_class: str
    type_name: str
    java_type: str
    signed: bool
    array: bool
    readonly: bool
    enum_name: str | None


def parse_meta_datapoint(label: str, meta: Mapping[str, Any]) -> MetaDataPoint:
    """Validate and wrap one entry of the ``init`` metaDataPoints map."""
    try:
        return MetaDataPoint(
            id=str(meta["name"]),
            code=meta.get("friendlyName"),
            gain=meta["gain"],
            unit=meta.get("unit"),
            obd_class=meta["obdClass"],
            type_name=meta["typeName"],
            java_type=meta["type"],
            signed=bool(meta["signed"]),
            array=bool(meta["array"]),
            readonly=bool(meta["readonly"]),
            enum_name=meta.get("enumName"),
        )
    except KeyError as error:
        raise ProtocolError(
            f"Metadata for datapoint {label} is missing field {error}"
        ) from error


def parse_int_token(raw: str) -> int:
    """Parse a whole-number wire token (integral decimals accepted)."""
    try:
        return int(Decimal(raw).to_integral_value())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"not an integer: {raw!r}") from error
