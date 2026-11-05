"""Catalogue of the documented MSR2 local keys.

Loaded from the packaged ``data/msr2-local-keys.json`` so wheels work
without repository-root access and a later Node.js implementation can
consume the identical checked-in JSON file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from importlib.resources import files
from types import MappingProxyType

from .models import Key, KeyType, Normalization

_EXPECTED_FIELDS = frozenset(
    {
        "key",
        "group",
        "name",
        "type",
        "documented_unit",
        "normalized_unit",
        "normalization",
        "notes",
        "source",
        "sentinel",
    }
)

AnyKey = Key[bool] | Key[int] | Key[Decimal] | Key[str]


def _load_local_keys() -> Mapping[str, AnyKey]:
    resource = files("senertec_dachs").joinpath("data/msr2-local-keys.json")
    records = json.loads(resource.read_text(encoding="utf-8"))
    catalogue: dict[str, AnyKey] = {}
    for record in records:
        if set(record) != _EXPECTED_FIELDS:
            unexpected = sorted(set(record) ^ _EXPECTED_FIELDS)
            raise ValueError(f"Catalogue record fields do not match the schema: {unexpected}")
        wire_key = record["key"]
        if wire_key in catalogue:
            raise ValueError(f"Duplicate catalogue key: {wire_key}")
        catalogue[wire_key] = Key(
            key=wire_key,
            group=record["group"],
            name=record["name"],
            type=KeyType(record["type"]),
            documented_unit=record["documented_unit"],
            normalized_unit=record["normalized_unit"],
            normalization=Normalization(record["normalization"]),
            notes=record["notes"],
            source=record["source"],
            sentinel=record["sentinel"],
        )
    return MappingProxyType(catalogue)


#: All documented readable MSR2 local keys, indexed by exact wire key.
LOCAL_KEYS: Mapping[str, AnyKey] = _load_local_keys()
