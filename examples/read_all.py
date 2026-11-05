"""Example: read every documented key from a Dachs MSR2 and print it readably.

Usage (from py/, with the library installed in .venv):
    .\.venv\Scripts\python.exe examples\read_all.py [host]

The host defaults to DACHS_HOST from the environment, then 192.168.1.50.

Demonstrates the two everyday entry points of senertec_dachs:
- ``read_snapshot()`` for the friendly typed summary at the top,
- the ``LOCAL_KEYS`` catalogue plus ``Reading.from_raw`` for a generic,
  complete listing without naming every field by hand.
"""

import asyncio
import os
import sys
from collections.abc import Mapping

from senertec_dachs import LOCAL_KEYS, LocalClient, Reading, ReadingState

DEFAULT_HOST = os.environ.get("DACHS_HOST", "192.168.1.50")


def format_reading(reading: Reading[object], unit: str | None) -> str:
    """One human-readable line for a typed reading."""
    if reading.state is ReadingState.AVAILABLE:
        return f"{reading.value} {unit or ''}".rstrip()
    if reading.state is ReadingState.MISSING:
        return "(missing — not equipped?)"
    if reading.state is ReadingState.UNAVAILABLE:
        return "(not connected)"
    if reading.state is ReadingState.UNVERIFIED:
        return f"(scale unverified) raw {reading.raw}"
    return f"(invalid) raw {reading.raw!r}"


def print_summary(snapshot) -> None:
    """The headline facts, via the typed snapshot attributes."""
    print("=== SenerTec Dachs (MSR2) ===")
    print(f"  Serial number:    {snapshot.identity.serial_number.value}")
    print(f"  Part number:      {snapshot.identity.part_number.value}")
    print(f"  Runtime:          {snapshot.operating.runtime.value} h")
    print(f"  Starts:           {snapshot.operating.starts.value}")
    print(f"  Energy electric:  {snapshot.operating.electrical_energy.value} kWh")
    print(f"  Energy thermal:   {snapshot.operating.thermal_energy.value} kWh")
    print(f"  Active power:     {snapshot.measurements.active_power.value} kW")
    print(f"  Engine speed:     {snapshot.measurements.engine_speed.value} rpm")
    print(f"  Outdoor temp:     {snapshot.temperatures.outdoor.value} °C")
    print(f"  Maintenance due:  {snapshot.maintenance.due.value}")


def print_all_readings(raw: Mapping[str, str]) -> None:
    """Every documented key, grouped, using the catalogue metadata."""
    current_group: str | None = None
    for key, descriptor in LOCAL_KEYS.items():
        if descriptor.group != current_group:
            current_group = descriptor.group
            print(f"\n--- {current_group.replace('_', ' ')} ---")
        reading = Reading.from_raw(descriptor, raw.get(key))
        unit = descriptor.normalized_unit or descriptor.documented_unit
        print(f"  {descriptor.name:<44} {format_reading(reading, unit)}")


async def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    async with LocalClient(host) as client:
        snapshot = await client.read_snapshot()  # one pass, all 118 keys

    print_summary(snapshot)
    print_all_readings(snapshot.raw)


asyncio.run(main())
