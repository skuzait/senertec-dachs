"""Publish MSR2 local or old Dachsportal readings to MQTT.

MSR2 local:     set DACHS_HOST.
Old Dachsportal: set DACHS_PORTAL_ID and DACHS_PORTAL_PASSWORD instead.
MQTT:   MQTT_HOST (default localhost), MQTT_PREFIX (default "dachs"),
        INTERVAL_SECONDS (default 60).

Requires paho-mqtt, which is NOT a dependency of senertec-dachs:

    pip install paho-mqtt

Publishes one retained JSON message per interval to <prefix>/state.
"""

import asyncio
import json
import os

import paho.mqtt.client as mqtt

from senertec_dachs import Dachs


def make_device() -> Dachs:
    if os.environ.get("DACHS_HOST"):
        return Dachs(os.environ["DACHS_HOST"])
    return Dachs.cloud(
        os.environ["DACHS_PORTAL_ID"], os.environ["DACHS_PORTAL_PASSWORD"]
    )


async def main() -> None:
    prefix = os.environ.get("MQTT_PREFIX", "dachs")
    interval = float(os.environ.get("INTERVAL_SECONDS", "60"))

    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    publisher.connect(os.environ.get("MQTT_HOST", "localhost"))
    publisher.loop_start()

    async with make_device() as dachs:
        while True:
            await dachs.update()
            state = {
                "running": dachs.running,
                "power_w": dachs.power_w,
                "engine_speed_rpm": dachs.engine_speed_rpm,
                "outdoor_temp_c": (
                    str(dachs.outdoor_temperature)
                    if dachs.outdoor_temperature is not None
                    else None
                ),
                "supply_temp_c": (
                    str(dachs.supply_temperature)
                    if dachs.supply_temperature is not None
                    else None
                ),
                "electrical_energy_kwh": (
                    str(dachs.electrical_energy_kwh)
                    if dachs.electrical_energy_kwh is not None
                    else None
                ),
                "thermal_energy_kwh": (
                    str(dachs.thermal_energy_kwh)
                    if dachs.thermal_energy_kwh is not None
                    else None
                ),
                "maintenance_due": dachs.maintenance_due,
                "device_online": dachs.device_online,
                "fetched_at": (
                    dachs.data_fetched_at.isoformat()
                    if dachs.data_fetched_at is not None
                    else None
                ),
            }
            publisher.publish(f"{prefix}/state", json.dumps(state), retain=True)
            print(f"published {prefix}/state")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
