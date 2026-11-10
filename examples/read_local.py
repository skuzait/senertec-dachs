"""Read a Dachs MSR2 through its local HTTP GLT interface.

The host is DACHS_HOST from the environment (default 192.168.1.50).
Run from py/ with the library installed:

    .\\.venv\\Scripts\\python.exe examples\\read_local.py
"""

import asyncio
import os

from senertec_dachs import Dachs


async def main() -> None:
    host = os.environ.get("DACHS_HOST", "192.168.1.50")
    async with Dachs(host) as dachs:
        await dachs.update()
        print(f"Serial:      {dachs.serial_number}")
        print(f"Running:     {dachs.running}")
        print(f"Power:       {dachs.power_w} W")
        print(f"Engine:      {dachs.engine_speed_rpm} rpm")
        print(f"Outdoor:     {dachs.outdoor_temperature} °C")
        print(f"Supply:      {dachs.supply_temperature} °C")
        print(f"Return:      {dachs.return_temperature} °C")
        print(f"Runtime:     {dachs.runtime_hours} h")
        print(f"Starts:      {dachs.starts}")
        print(f"Electrical:  {dachs.electrical_energy_kwh} kWh")
        print(f"Thermal:     {dachs.thermal_energy_kwh} kWh")
        print(f"Maintenance due: {dachs.maintenance_due}")


asyncio.run(main())
