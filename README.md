# senertec-dachs

[![PyPI](https://img.shields.io/pypi/v/senertec-dachs)](https://pypi.org/project/senertec-dachs/)
![Python](https://img.shields.io/pypi/pyversions/senertec-dachs)
![License](https://img.shields.io/pypi/l/senertec-dachs)

Async Python client for SenerTec Dachs combined heat and power units
(BHKWs). Supports local access to MSR2 and MSR3 controllers, the old
Dachsportal cloud, and the newer DachsConnect cloud.

## Installation

```sh
pip install senertec-dachs
```

Requires Python 3.11 or later. The only runtime dependency is `aiohttp`.

## Choose a connection

| Backend | Device or service | Entry point | Connection |
|---|---|---|---|
| `local` | MSR2 local access | `Dachs(host)` | HTTP, port 8080 |
| `cloud` | Old Dachsportal cloud, used by the DachsApp | `Dachs.cloud(portal_id, password)` | Portal ID and DachsApp password |
| `cloud2` | New DachsConnect / DachsPortal2 cloud | `Dachs.cloud2(email, password, serial=...)` | Account email and password |
| `glt` | MSR3 local access on Dachs Gen2 | `Dachs.glt(host)` | Modbus TCP, port 502 |

All four use `await dachs.update()` followed by property access. Available
readings and control methods depend on the backend. A property returns
`None` when that backend does not supply it; this does not mean zero or off.

## Read a device

This complete example reads an MSR2 unit on the local network:

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

`update()` performs the read and stores a snapshot. Properties read that
snapshot without making further requests. Call `update()` again to refresh
it. Accessing readings before the first successful update raises
`RuntimeError`.

Use an `async with` block to close the connection when finished. If you
create a client yourself and pass it to `Dachs(client=client)`, you are
responsible for closing that client.

The following snippets belong inside an async function, like `main()` above.

### MSR2 local access

Enable the GLT interface on the MSR2 Ethernet board. The client defaults
to port 8080, username `glt`, and an empty password. Supply the configured
password if your device requires one:

```python
async with Dachs("192.168.1.50", password="your-glt-password") as dachs:
    await dachs.update()
    print(dachs.serial_number, dachs.runtime_hours, dachs.starts)
```

This backend reads the MSR2 key catalogue: operating counters,
temperatures, maintenance, actuator states, multi-module status, and the
daily 15-minute run grid. It also supports raw key reads through
`dachs.client.read_keys([...])`.

### Old Dachsportal cloud (`cloud`)

Use the portal ID, which is the trailing part of the serial number, and
the password used by the DachsApp:

```python
async with Dachs.cloud("12345", "dachsapp-password") as dachs:
    await dachs.update()
    print(dachs.power_w)
    print(dachs.device_online)
    print(dachs.data_fetched_at)
```

The portal returns cached polls, which can be several minutes old.
`device_online` reports the portal's device status; `data_fetched_at` is
its last poll timestamp when provided. A successful portal request does
not necessarily mean the device is online.

Additional portal methods are available on `dachs.client`, inside the
connection's context:

```python
async with Dachs.cloud("12345", "dachsapp-password") as dachs:
    client = dachs.client
    reports = await client.reports()
    profile = await client.daily_profile()
    schedules = await client.schedules()
    statistics = await client.statistics("2026-9", "2026-11")
```

Statistics use inclusive `YYYY-M` month ranges. Reports may contain
operator contact details. The raw `client.metainfo()` response can include
a session token, so do not log it verbatim.

### New DachsConnect cloud (`cloud2`)

Use your DachsConnect / DachsPortal2 email and password. For a single-unit
account, the client selects the unit automatically. For an account with
multiple units, supply its serial number:

```python
async with Dachs.cloud2(
    "you@example.com", "your-password", serial="96385"
) as dachs:
    await dachs.update()
    print(dachs.power_w, dachs.electrical_energy_kwh)

    # Read all native CanIP datapoints, not just the shared properties.
    values = await dachs.client.read_datapoints()
    for code, reading in values.items():
        print(code, reading.value, reading.unit)

    print("Cached values used:", dachs.client.values_stale)
```

To discover serial numbers or switch between units, use `Cloud2Client`
directly:

```python
from senertec_dachs import Cloud2Client

async with Cloud2Client("you@example.com", "your-password") as client:
    units = await client.units()
    for unit in units:
        print(unit.serial, unit.model, unit.location_name)

    await client.switch_unit("96385")  # Use a serial from the list above.
    values = await client.read_datapoints()
```

`read_datapoints()` requests all datapoints listed for the selected unit.
Responses arrive over a WebSocket. Datapoints that do not respond are
omitted from the result. Each reading includes the raw value,
server-provided unit and gain, enum translation, and age. Cached replies
are retained and flagged by `client.values_stale`.

By default the read returns as soon as every datapoint has answered,
live or from the portal's cache; values may then be older, and each
reading's `age_seconds` plus `client.values_stale` tell you which came
from the cache. Pass `read_datapoints(wait_for_live=True)` to hold the
request open for live values only and accept the wait; on slow portals
a full live read can take minutes.

Pass `on_value=callback` to receive each value as its frame arrives on
the websocket, before the read as a whole completes.

### Datapoint names

The portal ships a translation table with a display name for almost
every datapoint (4117 of 4145 on the demo unit) in each supported
language:

```python
names = client.display_names("de-de")   # or "en-gb"; client default if omitted
print(names["IM009"])                   # 'Temperatur WR-G Modul Frequenzumrichter'
```

Every `CanIpValue` carries its `name` (None where the portal has no
entry). The names are the portal's own, verified against the Dachs Gen2
GLT manual (8098.003.000.09) for the documented identification codes.

### Background listener

```python
def on_value(value):
    print(value.board, value.code, value.value, value.unit)

client.start_listener(on_value, interval=60)   # every datapoint, every 60 s
...
client.stop_listener()
```

The listener requests datapoints on a fixed interval and invokes the
callback for each value frame the moment it arrives on the websocket;
errors are retried with backoff. Keep the callback fast — it runs on
the listener's event loop.

You can optionally pass codes to request a subset. Results use plain
codes such as `MM000`, board-qualified codes when needed to distinguish
duplicates, and indexed keys for array elements such as `AM029[0]`.

`dachs.update()` reads the subset mapped to the shared properties, not the
entire CanIP catalogue. MSR2-specific fields, including actuator bitfields
and MSR2 fault codes, return `None`. Native methods also include
`get_errors()` for board errors and `chart_names()` / `chart(name)` for
historical data.

### MSR3 local access (`glt`)

On a Dachs Gen2, enable the GLT interface using parameter NP221. This is a
Modbus TCP connection, not the MSR2 HTTP interface. Reads require no PIN.
Keep the interface on a trusted local network; do not expose port 502 to
the internet.

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

Each update reads registers 8000 through 8083 in one request. Poll no
faster than once per second. The shared properties include serial number,
power, runtimes, starts, energies, inlet/outlet/outdoor temperatures, and
requested module count. Energy counters have 0.1 kWh resolution.
Commissioning date, part number, maintenance state, and detailed MSR2
fault codes are not exposed by this register map.

Client methods such as `profile()` and `status()` use the last read;
they do not refresh it. `client.last_values` contains decoded register
integers and strings, **before** applying the catalogue's scale factors.

The implementation follows SenerTec's GLT specification
8098.119.000.02 and was checked against the community ioBroker register
map and a loopback Modbus server. It has not been tested on an MSR3 unit.

## Values, units, and missing readings

Power is exposed as whole watts through `power_w`. Energy counters use
kWh, runtimes use hours, and temperatures use °C. Fractional readings use
`decimal.Decimal` rather than binary floats.

For detailed availability information, inspect the snapshot returned by
`update()`:

```python
async with Dachs("192.168.1.50") as dachs:
    snapshot = await dachs.update()
    reading = snapshot.temperatures.hot_water
    print(reading.value, reading.state, reading.raw)
```

| State | Meaning |
|---|---|
| `AVAILABLE` | Parsed value with a verified unit |
| `MISSING` | The response did not contain the key |
| `INVALID` | The value could not be parsed; raw text is retained |
| `UNVERIFIED` | The scale or meaning has not been confirmed |
| `UNAVAILABLE` | A device sentinel, such as an unconnected temperature sensor |

Shared properties return a value only for `AVAILABLE` readings. The MSR2
catalogue contains 118 keys, including fields without a convenience
property. `snapshot.raw` retains the returned key/value texts. On newer
backends this contains only the mapped fields, not the full native data.

If an update raises `DachsError`, the previous snapshot is retained and
`device_online` becomes `False`. Do not treat the retained values as a
successful new read. For local reads, `data_fetched_at` records the update
time; it is not a device-supplied measurement timestamp.

## Control commands

Read access and control support differ between backends. These commands
operate physical equipment; check the controller configuration before
using them.

### MSR2 local and old Dachsportal

```python
async with Dachs("192.168.1.50") as dachs:
    await dachs.request_run()
    # Alternatively, request a specific number of modules:
    # await dachs.request_power(2)
```

`request_power(modules)` accepts 0 through 10 modules. External demand
expires after ten minutes unless renewed. An acknowledgement does not
prove that the engine started. The old-cloud `request_power()` path is
implemented but has not been confirmed on a real portal.

### MSR3 Modbus TCP

Use `GltClient.set_power_setpoint(watts)` and `set_locked(bool)` rather
than the module-based methods above. Writes require the configured GLT
PIN (NP222). The manual's screenshots show `1234`; check the setting on
your controller rather than assuming that value is unchanged.

The setpoint uses 10 W steps. The controller accepts setpoint changes at
most every five minutes and discards external control values if they are
not renewed within ten minutes. `start_heartbeat()` returns a task that
repeats the last written setpoint and lock state every 300 seconds:

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

Cancel and await the heartbeat before closing the connection. Cancellation
stops renewal; it is not an immediate engine-stop command. A Modbus write
echo confirms delivery, not PIN validity or controller acceptance.

### DachsConnect cloud

`write_datapoint()` provides an experimental native write interface. It
has not been verified against a live account. `request_run()` and
`request_power(modules)` raise `WriteError` on both the `cloud2` and `glt`
backends because their control interfaces differ from MSR2.

## Error handling

Catch `DachsError` for library errors. Subclasses include
`AuthenticationError`, `ConnectionError`, `RequestTimeoutError`,
`HttpError`, `ProtocolError`, `WriteError`, and `ModbusError`. Invalid
arguments may raise `ValueError`.

For write failures, inspect `error.outcome_unknown` before deciding what
to do next. A command may have reached the controller even if its reply
was lost. Do not blindly retry it.

## Protocol notes

- Two MSR2 runtime keys are documented as seconds but report hours.
  The library returns hours.
- Old Dachsportal values may differ slightly from local readings because
  the portal calculates with binary floats and returns cached polls.
- The old portal's thermal-energy counter includes the condenser share;
  the local MSR2 counter reports the CHP unit alone.

### Unverified MSR2 fields

The following ten keys remain `UNVERIFIED`. Raw values are available,
but the library does not expose them as scaled properties.

| Key | Meaning to confirm |
|---|---|
| `BD3112.Ww_Bd.ulWwMengepA` | Annual hot-water volume and its unit |
| `Hka_Mw1.Aktor.bWwPumpe` | Hot-water loading pump output and its scale |
| `Mm_MinMax.sBhMaxWart` / `sBhMinWart` | Maximum/minimum hours until maintenance |
| `Mm_MinMax.ModulBhMax.ulWert` / `ModulBhMin.ulWert` | Maximum/minimum module operating hours |
| `Hka_Ew.HydraulikNr.bSpeicherArt` / `bWW_Art` / `b2_Waermeerzeuger` / `bMehrmodul` | Hydraulic configuration codes |

To help verify them, read the keys with `dachs.client.read_keys([...])`
on MSR2 or the old portal and compare them with the controller display.
An issue with the raw response, displayed value, controller version, and
relevant operating state is useful evidence. Remove credentials and
personal details before sharing captures.

## Examples

Each script documents its environment variables at the top. Run them with
the library installed, for example `python examples/read_local.py`.

| Script | Purpose |
|---|---|
| [read_local.py](examples/read_local.py) | MSR2 local readings (`DACHS_HOST`) |
| [read_cloud.py](examples/read_cloud.py) | Old Dachsportal (`DACHS_PORTAL_ID`, `DACHS_PORTAL_PASSWORD`) |
| [read_cloud2.py](examples/read_cloud2.py) | DachsConnect shared properties and all native datapoints (`DACHS_CONNECT_EMAIL`, `DACHS_CONNECT_PASSWORD`, optional `DACHS_CONNECT_SERIAL`) |
| [read_glt.py](examples/read_glt.py) | MSR3 local readings (`DACHS_GLT_HOST`, optional `DACHS_GLT_PIN`) |
| [read_all.py](examples/read_all.py) | Every MSR2 catalogue key and its reading state |
| [mqtt_publish.py](examples/mqtt_publish.py) | MSR2 local or old Dachsportal readings to MQTT; requires `paho-mqtt` |

The four `read_*` backend examples do not send control commands.

## Development

```sh
python -m venv .venv
# Activate the virtual environment, then:
python -m pip install -e ".[test]"
python -m pytest -q
```

The tests use loopback HTTP, WebSocket, and Modbus servers; no device or
portal credentials are required. The MSR2 protocol fixture is in
`tests/fixtures/local-protocol.json`.

## License

[MIT](LICENSE).
