"""Native value types for DachsConnect (cloud v2, CanIP datapoints).

The cloud v2 backend has its own datapoint universe (CanIP, addressed by
board and by the stable codes printed in the Dachs Gen2 operating
manual, e.g. ``MM000``). These types carry that universe directly: a
:meth:`~senertec_dachs.cloud2.client.Cloud2Client.read_datapoints` value
keeps the server-provided gain, unit and enumeration translation so no
information is lost, and the first-generation access paths stay
unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class UnitInfo:
    """One energy unit of the account (``/rest/info/units`` entry)."""

    serial: str
    model: str
    product_group: str
    online: bool | None
    connected: bool | None
    mehrmodul: bool | None
    leitregler: bool | None
    external: bool | None
    article_number: str | None
    commissioned_ms: int | None
    location_name: str | None
    location_city: str | None
    location_postal_code: str | None
    location_street: str | None
    contact_person: str | None


@dataclass(frozen=True, slots=True)
class CanIpValue:
    """One CanIP datapoint reading with its metadata.

    ``value`` is the gain-applied, exact :class:`Decimal` — or the
    translated text for datapoints with an enumeration. ``raw`` is the
    untouched wire value. ``age_seconds`` is 0 for live values and
    greater for the portal's cached last-poll values.
    """

    code: str
    board: str
    id: str
    raw: object
    value: Decimal | str | bool | None
    unit: str | None
    gain: Decimal | None
    enum_name: str | None
    age_seconds: int
    readable: bool | None = None
    name: str | None = None
    """Human name from the server translation table, when available."""


@dataclass(frozen=True, slots=True)
class CanIpError:
    """One Gen2 error entry of a board (``canipErrors``)."""

    board: str
    code_text: str
    category: int
    code: int
    current: bool
    valid: bool
    timestamp_ms: int | None
    counter: int | None
    category_text: str | None
    translation: str | None


@dataclass(frozen=True, slots=True)
class Cloud2Status:
    """What DachsConnect says about the connected unit.

    ``device_online`` is the unit payload's ``online`` flag, ``connected``
    whether the unit has a live CanIP connection for this session; values
    may be the portal's cached last poll even when these are ``False``.
    """

    device_online: bool | None
    connected: bool | None
    product_group: str | None
    mehrmodul: bool | None
    board_names: tuple[str, ...] = ()
    fetched_at: datetime | None = None

    @classmethod
    def from_unit(cls, unit: Mapping[str, object]) -> "Cloud2Status":
        """Build from the ``/rest/canip/{serial}`` payload."""
        boards = unit.get("boards")
        fetched_at = datetime.now(timezone.utc)
        return cls(
            device_online=unit.get("online")
            if isinstance(unit.get("online"), bool)
            else None,
            connected=unit.get("connected")
            if isinstance(unit.get("connected"), bool)
            else None,
            product_group=unit.get("productGroup") or None,
            mehrmodul=unit.get("mehrmodul")
            if isinstance(unit.get("mehrmodul"), bool)
            else None,
            board_names=tuple(
                board["name"]
                for board in boards
                if isinstance(board, Mapping) and board.get("name")
            )
            if isinstance(boards, list)
            else (),
            fetched_at=fetched_at,
        )
