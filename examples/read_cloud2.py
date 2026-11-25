"""Read a Dachs through the new DachsConnect / DachsPortal2 cloud.

Environment variables:
    DACHS_CONNECT_EMAIL     account email
    DACHS_CONNECT_PASSWORD  account password
    DACHS_CONNECT_SERIAL    unit serial; required for multi-unit accounts

Reads the shared properties, then all native CanIP datapoints. This
example does not send control commands.
"""

import asyncio
import os
import sys

# cloud2 (DachsConnect) is not published yet; the example and the
# subpackage stay for when it ships.
from senertec_dachs import Dachs
from senertec_dachs.cloud2 import Cloud2Client


async def main() -> None:
    email = os.environ.get("DACHS_CONNECT_EMAIL")
    password = os.environ.get("DACHS_CONNECT_PASSWORD")
    if not email or not password:
        sys.exit("Set DACHS_CONNECT_EMAIL and DACHS_CONNECT_PASSWORD first")
    serial = os.environ.get("DACHS_CONNECT_SERIAL") or None

    async with Dachs.cloud2(email, password, serial=serial) as dachs:
        await dachs.update()

        print(f"Unit:                  {dachs.serial_number}")
        print(f"Portal reaches device: {dachs.device_online}")
        print(f"Running:               {dachs.running}")
        print(f"Power:                 {dachs.power_w} W")
        print(f"Runtime total:         {dachs.runtime_hours} h")
        print(f"Starts:                {dachs.starts}")
        print(f"Electrical (total):    {dachs.electrical_energy_kwh} kWh")
        print(f"Thermal (total):       {dachs.thermal_energy_kwh} kWh")
        print(f"Engine runtime:        {dachs.engine_runtime_hours} h")
        print(f"Outdoor:               {dachs.outdoor_temperature} °C")
        print(f"Supply temp:           {dachs.supply_temperature} °C")

        # Read all native datapoints; print a few alongside the shared
        # values. By default the read returns as soon as every datapoint
        # has answered, live or from the portal cache; pass
        # wait_for_live=True when you need guaranteed-live values and
        # accept the wait.
        client = dachs.client
        assert isinstance(client, Cloud2Client)
        values = await client.read_datapoints()
        print(f"Datapoints read:       {len(values)}")
        for code in ("MM000", "MC001", "MC002"):
            value = values.get(code) or values.get(f"CU-CHP01.{code}")
            if value is not None:
                print(f"CanIP {code:8s} {value.value} {value.unit or ''} "
                      f"(board {value.board})")

        errors = await client.get_errors()
        for error in errors:
            print(
                f"Error {error.code_text}: {error.translation or error.code_text} "
                f"({error.board})"
            )

        names = await client.chart_names()
        if names:
            chart = await client.chart(names[0])
            print(f"Chart {names[0]}: {len(chart.get('boardMap', {}))} boards")


if __name__ == "__main__":
    asyncio.run(main())
