"""Curated display filter for native CanIP datapoints.

The CanIP universe of a connected unit contains well over a thousand
datapoints, and most secrets that a home dashboard should never show:
per-board mirrors of the same signal, permanently unset telemetry
(battery datapoints on a unit without one), the portal's own debug
datapoints (random token names, HTML test strings, base64 blobs),
raw epoch-millisecond timestamps and configuration counters.

This module keeps what a human would actually monitor. The rules are
admission rules: a datapoint that fails them never becomes visible,
so consumers can create their representations lazily on the first
admitted value.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Iterable

from .models import CanIpValue

__all__ = ["CanIpDisplayFilter", "DEFAULT_UNITS"]

#: Physical measurement units a dashboard shows by default. Anything
#: else (money, none at all, exotic engineering units) stays library-only.
DEFAULT_UNITS = frozenset(
    {
        "°C",   # temperature
        "°CA",  # crank angle degrees (ignition timing)
        "W", "kW",          # power
        "kWh", "Wh",        # energy
        "%",                # percentage
        "V", "A",           # electrical
        "Hz",               # frequency / PWM
        "Rpm", "RPM", "rpm",  # engine speed
        "h",                # hours
        "Min", "min", "s",  # durations
        "Days", "Day",
        "bar",              # pressure
        "l", "m", "kg",     # volume, length, mass
        "Nm",               # torque
        "Step",
    }
)

_BOARD_PRIORITY = ("CU-", "SU", "EM", "INV", "WR-G", "WR-N")

#: Random portal debug tokens (``tmyRBbn48McrM``): long, no separators
#: or spaces. Real display names contain spaces or common punctuation.
_TOKEN_NAME = re.compile(r"^[A-Za-z0-9+/=]{16,}$")

#: Name parts that suggest a timestamp datapoint.
_TIME_HINT = re.compile(r"time|date|stamp", re.IGNORECASE)

#: Smallest plausible Millis-since-epoch value we accept (about 1983).
#: Larger legit integers seen in the wild (tokens, descriptors) stay
#: well below 1e11; epoch values above 1e14 (year 33658) never occur.
_EPOCH_MS_MIN = 10**11
_EPOCH_MS_MAX = 10**14


def _board_rank(board: str) -> int:
    """Sort key that puts primary boards (CU-CHP01) before mirrors."""
    for rank, prefix in enumerate(_BOARD_PRIORITY):
        if board.startswith(prefix):
            return rank
    return len(_BOARD_PRIORITY)


def _element_index(code: str) -> int | None:
    """Array element index from ``CODE[n]``, if present."""
    if "[" not in code:
        return None
    try:
        return int(code.rsplit("[", 1)[1].rstrip("]"))
    except (ValueError, IndexError):
        return None


def _display_key(value: CanIpValue) -> tuple[str, int | None]:
    """Dedupe identity: same human name + same array element."""
    name = value.name or value.code
    return (name.casefold(), _element_index(value.code))


class CanIpDisplayFilter:
    """Curated admission logic for a stream of :class:`CanIpValue`.

    Typical use: feed every value of a full read and every listener
    frame through :meth:`admit`; create a display element only for
    values that survive (lazy creation — unset datapoints that never
    carry a value produce nothing).

    Rules, in order:

    1. The datapoint needs a server display name; nonsense token names
       are rejected.
    2. Mirror boards never double a signal: the first admitted value
       for a (name, element) pair wins, and values are processed in
       board-priority order (CU-CHP01 before accessory boards).
    3. Only measurable scalars pass: :class:`Decimal` values with a
       unit from :data:`DEFAULT_UNITS`, plus epoch timestamps which
       become :class:`datetime` (UTC). Translated enumeration values
       (bounded, server-defined states like ``Standby``) are kept;
       free-form strings, bools and unit-less numbers are not.
    """

    def __init__(self, *, allowed_units: Iterable[str] | None = None) -> None:
        self._units = frozenset(allowed_units) if allowed_units is not None else DEFAULT_UNITS
        self._admitted: dict[tuple[str, int | None], str] = {}

    @property
    def admitted(self) -> dict[tuple[str, int | None], str]:
        """(display name, element index) -> board, for debug visibility."""
        return dict(self._admitted)

    def admit(self, value: CanIpValue) -> CanIpValue | None:
        """Return the value shaped for display, or ``None`` to reject."""
        name = value.name
        if not name or not isinstance(name, str):
            return None
        if "<" in name or len(name) > 64 or _TOKEN_NAME.match(name):
            return None
        key = _display_key(value)
        first = self._admitted.get(key)
        if first is None:
            # Batch input is pre-sorted by board priority, so stream
            # consumers (per-frame admission) keep exactly one element
            # per (name, element) identity across mirror boards.
            self._admitted[key] = value.board
        else:
            return None

        raw = value.raw
        # Translated enumeration states (``Standby``, ``Inactive`` …).
        if value.enum_name is not None and isinstance(value.value, (str, bool)):
            return value
        if value.enum_name is None and isinstance(value.value, str):
            return None  # free-form text (debug/HTML/blob datapoints)
        if isinstance(value.value, bool):
            return None
        if not isinstance(value.value, Decimal):
            return None  # compound/structured values never have one
        if value.unit in self._units:
            return value
        if value.unit is None and _TIME_HINT.search(name):
            if isinstance(raw, int) and _EPOCH_MS_MIN <= raw < _EPOCH_MS_MAX:
                return _as_datetime(value)
        return None

    def filter(self, values: Iterable[CanIpValue]) -> list[CanIpValue]:
        """Admit a batch, in board-priority order."""
        ordered = sorted(values, key=lambda v: _board_rank(v.board))
        out: list[CanIpValue] = []
        for value in ordered:
            admitted = self.admit(value)
            if admitted is not None:
                out.append(admitted)
        return out


def _as_datetime(value: CanIpValue) -> CanIpValue:
    """Rebuild a value with a UTC datetime in place of the ms integer."""
    assert isinstance(value.raw, int)
    return CanIpValue(
        code=value.code,
        board=value.board,
        id=value.id,
        raw=value.raw,
        value=dt.datetime.fromtimestamp(value.raw / 1000, tz=dt.timezone.utc),
        unit=None,
        gain=value.gain,
        enum_name=None,
        age_seconds=value.age_seconds,
        readable=value.readable,
        name=value.name,
    )
