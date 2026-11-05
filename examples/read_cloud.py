"""Example: read the same state through the SenerTec Dachsportal (cloud).

No local network access needed. Set the environment:

    DACHS_PORTAL_ID       trailing part of the Dachs serial number
    DACHS_PORTAL_PASSWORD portal password (the DachsApp's password)

Run from py/ with the library installed:

    .\\.venv\\Scripts\\python.exe examples\\read_cloud.py
"""

import asyncio
import os

from senertec_dachs import Dachs


async def main() -> None:
    portal_id = os.environ["DACHS_PORTAL_ID"]
    password = os.environ["DACHS_PORTAL_PASSWORD"]
    async with Dachs.cloud(portal_id, password) as dachs:
        await dachs.update()
        print(f"Portal reaches device: {dachs.device_online}")
        print(f"Data fetched at:       {dachs.data_fetched_at}")
        print(f"Serial:                {dachs.serial_number}")
        print(f"Running:               {dachs.running}")
        print(f"Power:                 {dachs.power_kw} kW")
        print(f"Outdoor:               {dachs.outdoor_temperature} °C")
        print(f"Electrical:            {dachs.electrical_energy_kwh} kWh")


asyncio.run(main())
