"""Read a latest-generation Dachs (MSR3) through its GLT Modbus interface.

Requires the GLT-Schnittstelle to be enabled on the unit (System menu,
NP221). The interface is plain Modbus TCP on the unit's Ethernet port,
intended for the local network only (spec 8098.119.000.02, 4.2: no
external exposure).

Environment variables:
    DACHS_GLT_HOST   the unit's IP in the local network
    DACHS_GLT_PIN    the 4-digit GLT pin (optional; only needed for
                     writes if a pin is configured)

Reads work without credentials. The example below only reads; the
write methods on the client (set_power_setpoint, set_locked) are the
documented GLT control path and are deliberately not exercised here.
"""

import asyncio
import os
import sys

from senertec_dachs import Dachs, GltClient


async def main() -> None:
    host = os.environ.get("DACHS_GLT_HOST")
    if not host:
        sys.exit("Set DACHS_GLT_HOST to the unit's IP address first")
    pin = int(os.environ["DACHS_GLT_PIN"]) if os.environ.get("DACHS_GLT_PIN") else None

    async with Dachs.glt(host, pin=pin) as dachs:
        await dachs.update()

        client = dachs.client
        assert isinstance(client, GltClient)
        profile = client.profile()
        status = client.status()

        print(f"Profile:     {profile.device_type_name} "
              f"(GLT interface {profile.glt_version})")
        print(f"Serial:      {profile.serial_number}")
        print(f"Rated power: {profile.rated_power_watts} W")
        print(f"Status:      {status.plant_status_text}")
        print(f"Demand:      {status.request_kind_text}")
        print(f"Last stop:   {status.shutdown_reason_text}")
        print(f"Running:     {dachs.runtime_hours} h total")
        print(f"Power:       {status.electric_output_watts} W")
        print(f"Energy:      {dachs.electrical_energy_kwh} kWh electric, "
              f"{dachs.thermal_energy_kwh} kWh thermal")
        print(f"Engine:      {dachs.engine_runtime_hours} h since start")
        print(f"Temps:       outdoor {dachs.outdoor_temperature} °C, "
              f"inlet {dachs.generator_inlet_temperature} °C, "
              f"outlet {dachs.supply_temperature} °C")

        counts = client.module_counts()
        print(f"Modules:     {counts.running} of {counts.available} running, "
              f"{counts.requested} requested")

        second = client.second_generator()
        print(f"2nd heater:  {second.status_text} "
              f"({second.hours} h, {second.starts} starts)")


if __name__ == "__main__":
    asyncio.run(main())
