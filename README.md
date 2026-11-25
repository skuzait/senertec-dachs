# senertec-dachs

[![PyPI](https://img.shields.io/pypi/v/senertec-dachs)](https://pypi.org/project/senertec-dachs/)
![Python](https://img.shields.io/pypi/pyversions/senertec-dachs)
![License](https://img.shields.io/pypi/l/senertec-dachs)

Read and control SenerTec Dachs units from Python. Use the local MSR2
interface or the older Dachsportal used by the DachsApp. Local MSR3
support is experimental. The newer DachsConnect portal is not supported
in this release.

## Installation

```sh
python -m pip install senertec-dachs
```

Requires Python 3.11 or later.

## Choose a connection

| Connection | Use it for | Python |
| --- | --- | --- |
| MSR2 on your local network | GLT over HTTP, port 8080 | `Dachs(host)` |
| Older Dachsportal | DachsApp access with a portal ID and password | `Dachs.cloud(portal_id, password)` |
| MSR3 / Gen2 on your local network (experimental) | GLT over Modbus TCP, port 502 | `Dachs.glt(host)` |

## Read a device

Enable GLT on your MSR2 Ethernet board and replace `192.168.1.50` with
your unit's IP address. The default username is `glt`. If you set a GLT
password, pass it as `password=` when creating `Dachs`.

```python
import asyncio

from senertec_dachs import Dachs


async def main() -> None:
    async with Dachs("192.168.1.50") as dachs:
        await dachs.update()
        print("Power:", dachs.power_w, "W")
        print("Running:", dachs.running)
        print("Outdoor temperature:", dachs.outdoor_temperature, "°C")
        print("Electrical energy:", dachs.electrical_energy_kwh, "kWh")
        print("Maintenance due:", dachs.maintenance_due)


asyncio.run(main())
```

`update()` reads the unit. Properties use that snapshot without making
another request, so call `update()` again when you need fresh values.
Before the first successful update, reading a property raises
`RuntimeError`. A property returns `None` if the connection does not
provide a reading.

`async with` closes the connection when you leave the block. If you pass
your own client with `Dachs(client=client)`, close that client yourself.

The examples below go inside an `async` function like `main()`.

For raw MSR2 keys, use `await dachs.client.read_keys([...])` inside the
connection block.

### Older Dachsportal

Replace the example credentials with your portal ID (the last part of
the serial number) and your DachsApp password.

```python
async with Dachs.cloud("12345", "dachsapp-password") as dachs:
    await dachs.update()
    print(dachs.power_w)
    print(dachs.device_online)
    print(dachs.data_fetched_at)
```

The portal caches readings, so they can be several minutes old. Check
`device_online` for the unit's status and `data_fetched_at` for the last
portal poll, when available. A successful request to the portal does not
mean the unit itself is online.

Reports, profiles, schedules, and statistics are available on `dachs.client`:

```python
async with Dachs.cloud("12345", "dachsapp-password") as dachs:
    client = dachs.client
    reports = await client.reports()
    profile = await client.daily_profile()
    schedules = await client.schedules()
    statistics = await client.statistics("2026-9", "2026-11")
```

Statistics take an inclusive range of months in `YYYY-M` format.
Reports may contain contact details, and `client.metainfo()` may
include a session token. Do not log or share those responses as is.

### MSR3 local access (`glt`)

**Experimental:** I still need a Dachs Gen2 unit to test this on. If you
have one and can try it, please [open an issue](https://github.com/skuzait/senertec-dachs/issues).

Enable GLT with parameter NP221 on the controller. You do not need a PIN
to read values. Keep Modbus TCP on your local network. Do not expose port
502 to the internet.

```python
async with Dachs.glt("192.168.1.42") as dachs:
    await dachs.update()
    print(dachs.power_w, dachs.runtime_hours, dachs.electrical_energy_kwh)

    client = dachs.client
    print(client.profile().serial_number)
    print(client.status().plant_status_text)
    print(client.status().shutdown_reason_text)
    print(client.module_counts())
    print(client.second_generator())
```

Each update reads registers 8000 through 8083. Poll at most once per
second. You can read the serial number, power, runtimes, starts, energy,
inlet, outlet and outdoor temperatures, and requested module count.
Energy counters have 0.1 kWh resolution. This register map does not
provide the commissioning date, part number, maintenance state, or
detailed MSR2 fault codes.

`profile()` and `status()` use the last read. `client.last_values` has
the decoded registers before scaling. The library follows SenerTec's GLT
specification 8098.119.000.02.

## Values, units, and missing readings

`power_w` uses whole watts. Energy is in kWh, runtime in hours, and
temperature in °C. Fractional values use `decimal.Decimal`.

To see why a reading is missing, check its state in the snapshot returned
by `update()`:

```python
async with Dachs("192.168.1.50") as dachs:
    snapshot = await dachs.update()
    reading = snapshot.temperatures.hot_water
    print(reading.value, reading.state, reading.raw)
```

| State | Meaning |
|---|---|
| `AVAILABLE` | Value with a known unit |
| `MISSING` | The response did not include the key |
| `INVALID` | The library could not parse the value |
| `UNVERIFIED` | The scale or meaning is not confirmed |
| `UNAVAILABLE` | The unit sent a special value, such as the code for a disconnected temperature sensor |

Properties return a value only for `AVAILABLE` readings. `snapshot.raw`
contains the raw key and value strings. On MSR3 it covers only the fields
the library maps to shared properties.

If `update()` raises `DachsError`, the library keeps the previous snapshot
and sets `device_online` to `False`. Those values are from the last
successful read. For local reads, `data_fetched_at` is the time the
library read the device, not when the device measured the value.

## Control commands

Check the controller settings before sending a command. These commands
can start the unit or change its power demand.

### MSR2 local and old Dachsportal

```python
async with Dachs("192.168.1.50") as dachs:
    await dachs.request_run()
    # Alternatively, request a specific number of modules:
    # await dachs.request_power(2)
```

`request_power(modules)` accepts 0 to 10 modules. External demand expires
after ten minutes unless renewed. A reply does not mean the engine
started. `request_power()` has not been tested on the live Dachsportal.

### MSR3 Modbus TCP

For MSR3, use `GltClient.set_power_setpoint(watts)` and `set_locked(bool)`.
To write values, pass the PIN configured in NP222 to `Dachs.glt()`.

Setpoints use steps of 10 W. The controller accepts a new setpoint at
most once every five minutes and drops external control values after ten
minutes without renewal. `start_heartbeat()` repeats the last setpoint
and lock state every 300 seconds:

```python
from contextlib import suppress

async with Dachs.glt("192.168.1.42", pin=1234) as dachs:
    client = dachs.client
    await client.set_power_setpoint(2500)
    heartbeat = client.start_heartbeat()
    try:
        await asyncio.sleep(600)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
```

Cancel and await the heartbeat before closing the connection. This stops
renewal but does not stop the engine immediately. A Modbus reply confirms
delivery, not that the PIN was correct or that the unit applied the change.

## Error handling

Catch `DachsError` for library errors. Subclasses include
`AuthenticationError`, `ConnectionError`, `RequestTimeoutError`,
`HttpError`, `ProtocolError`, `WriteError`, and `ModbusError`. Invalid
arguments may raise `ValueError`.

If a write fails, check `error.outcome_unknown` before trying again. If
it is `True`, the controller may have received the command even though
you did not get a reply.

## Protocol notes

* Two MSR2 runtime keys report hours even though the spec says seconds.
  The library returns hours.
* Dachsportal caches readings and uses binary floats. Its values may
  differ slightly from local readings.
* The portal includes condenser heat in its thermal energy total. MSR2
  local reports heat from the CHP unit alone.

### Unverified MSR2 fields

These keys are available as raw values, but the library does not scale
them.

| Key | Meaning to confirm |
|---|---|
| `BD3112.Ww_Bd.ulWwMengepA` | Annual hot water volume and its unit |
| `Hka_Mw1.Aktor.bWwPumpe` | Hot water loading pump output and its scale |
| `Mm_MinMax.sBhMaxWart` / `sBhMinWart` | Maximum/minimum hours until maintenance |
| `Mm_MinMax.ModulBhMax.ulWert` / `ModulBhMin.ulWert` | Maximum/minimum module operating hours |
| `Hka_Ew.HydraulikNr.bSpeicherArt` / `bWW_Art` / `b2_Waermeerzeuger` / `bMehrmodul` | Hydraulic configuration codes |

If you can confirm a value, read the key on MSR2 or Dachsportal with
`dachs.client.read_keys([...])` and compare it with the controller
display. In your issue, include the raw and displayed values, the
controller version, and what the unit was doing. Remove credentials and
personal information first.

## Examples

The scripts list their environment variables at the top. Once the
library is installed, work from the repository root and run a script:

```sh
python examples/read_local.py
```

| Script | Purpose |
|---|---|
| [read_local.py](examples/read_local.py) | MSR2 local readings (`DACHS_HOST`) |
| [read_cloud.py](examples/read_cloud.py) | Old Dachsportal (`DACHS_PORTAL_ID`, `DACHS_PORTAL_PASSWORD`) |
| [read_glt.py](examples/read_glt.py) | MSR3 local readings (`DACHS_GLT_HOST`, optional `DACHS_GLT_PIN`) |
| [read_all.py](examples/read_all.py) | Every MSR2 catalogue key and its reading state |
| [mqtt_publish.py](examples/mqtt_publish.py) | MSR2 local or old Dachsportal readings to MQTT; requires `paho-mqtt` |

The `read_*` examples do not send control commands.

## Development

```sh
python -m venv .venv
# Activate the virtual environment, then:
python -m pip install -e ".[test]"
python -m pytest -q
```

Tests run against local HTTP, WebSocket, and Modbus servers. You do not
need a Dachs unit or portal credentials.

## License

[MIT](LICENSE).
