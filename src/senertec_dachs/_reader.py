"""Shared reading logic used by both the local and the cloud client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .keys import LOCAL_KEYS
from .models import Reading, Snapshot


def normalize_requested_keys(keys: Sequence[str]) -> list[str]:
    """Validate keys and deduplicate while retaining first-seen order."""
    requested: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if not isinstance(key, str) or key == "":
            raise ValueError("keys must be non-empty strings")
        if any(character.isspace() for character in key):
            raise ValueError(f"keys must not contain whitespace: {key!r}")
        if key not in seen:
            seen.add(key)
            requested.append(key)
    return requested


class SnapshotReaderMixin:
    """Adds ``read_readings``/``read_snapshot`` to any client with ``read_keys``.

    Both backends return raw wire-text values from ``read_keys``; the typed
    model on top of that is shared, so a snapshot means the same thing
    regardless of where the values came from.
    """

    async def read_readings(self, keys: Sequence[str]) -> dict[str, Reading[Any]]:
        """Read the given documented keys as typed :class:`Reading` objects."""
        requested = normalize_requested_keys(keys)
        unknown = [key for key in requested if key not in LOCAL_KEYS]
        if unknown:
            raise ValueError(
                f"Key is not in the documented catalogue "
                f"(use read_keys for raw access): {unknown[0]!r}"
            )
        raw = await self.read_keys(requested)
        return {
            key: Reading.from_raw(LOCAL_KEYS[key], raw.get(key)) for key in requested
        }

    async def read_snapshot(self) -> Snapshot:
        """Read all documented keys and return them as one typed snapshot."""
        raw = await self.read_keys(list(LOCAL_KEYS))
        return Snapshot.from_raw(raw)
