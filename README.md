# senertec-dachs

Async Python library for the SenerTec Dachs combined heat and power unit.

One `Dachs` device object, two backends, the same API everywhere:

- **Local**: the MSR2 GLT interface on your LAN (HTTP, port 8080, Basic
  auth `glt`). Live values, direct writes.
- **Cloud**: the SenerTec Dachsportal — the backend the official DachsApp
  uses. Works from anywhere; values are the portal's cached polls of the
  device, so freshness is surfaced explicitly (`device_online`,
  `data_fetched_at`).

Typed readings for all 118 documented MSR2 keys, honest units
(unevidenced scales surface as `UNVERIFIED` with the raw value preserved,
never a guess), and safe writes with `outcome_unknown` semantics —
uncertain outcomes are never retried blindly.

## Install

```bash
pip install senertec-dachs
```

or straight from GitHub:

```bash
pip install "senertec-dachs @ git+https://github.com/skuzait/senertec-dachs"
```

Requires Python 3.11+, depends only on aiohttp.

## Local quickstart

```python
import asyncio
from senertec_dachs import Dachs

async def main() -> None:
    async with Dachs("192.168.1.50") as dachs:   # MSR2 address, port 8080 default
        await dachs.update()

        print(dachs.power_kw)            # Decimal or None
        print(dachs.running)             # True/False/None
        print(dachs.outdoor_temperature) # °C as Decimal
        print(dachs.electrical_energy_kwh, dachs.thermal_energy_kwh)
        print(dachs.maintenance_due)

asyncio.run(main())
```

`Dachs(host)` accepts an IP or hostname. Properties return `None` unless
the reading is cleanly available; the ten keys whose scale is still
unverified have no property — use `dachs.snapshot` (all 118 keys with
states and raw text) or `dachs.client` (raw reads) for those.

## Cloud quickstart

```python
async with Dachs.cloud("12345", "portal-password") as dachs:
    await dachs.update()
    print(dachs.power_kw)        # same properties as local
    print(dachs.device_online)   # could the portal reach the unit?
    print(dachs.data_fetched_at) # UTC timestamp of the portal's last poll
```

The portal ID is the trailing part of the Dachs serial number; the
password is the DachsApp password. No local network access needed.
Cloud extras live on the client:
`reports()`, `daily_profile()`, `schedules()`, `statistics()`,
`metainfo()`, `portal_news()`.

Differences worth knowing: portal values are cached polls (check
`device_online`), small rounding differences vs local are normal, and
the portal's `ulArbeitThermHka` includes the condenser share while the
local key reports the unit alone.

## Demand operations

Both calls work identically on both backends:

```python
await dachs.request_run()      # one-shot "run, please"
await dachs.request_power(2)   # per-module external demand, 0-10
```

- `request_run()` — local: the documented GLT demand with one module;
  cloud: the app's `dachsAnfordern` (portal default demand).
- `request_power(n)` — the documented `Stromf_Ew.Anforderung_GLT` write
  on both backends. The cloud path is unconfirmed against a real portal —
  treat its first real use as an experiment.

A demand is valid for ten minutes; renew sooner for sustained demand.
A clean acknowledgement proves acceptance, not that a module started.
Errors on writes carry `outcome_unknown=True` when the outcome cannot be
confirmed; nothing is retried automatically.

## Reading states

Every reading is a `Reading` with `raw` (wire text), `value`, `state`:

| State | Meaning |
|---|---|
| `AVAILABLE` | Clean typed value. |
| `MISSING` | Key absent (optional equipment). |
| `INVALID` | Malformed wire value; `raw` preserved. |
| `UNVERIFIED` | Well-formed, but the scale/unit is not evidenced; `raw` preserved. |
| `UNAVAILABLE` | Device sentinel (temperature sensors return `127` where not connected). |

## MQTT

`examples/mqtt_publish.py` publishes a JSON state message on an interval,
from either backend (`pip install paho-mqtt`, not a library dependency):

```bash
DACHS_HOST=192.168.1.50 MQTT_HOST=mosquitto.local python examples/mqtt_publish.py
```

More examples: `examples/read_local.py`, `examples/read_cloud.py`,
`examples/read_all.py` (full typed dump of all 118 keys).

## Development

```bash
py -m venv .venv
.venv\Scripts\pip install -e ".[test]"
.venv\Scripts\pytest -q
```

Tests run against loopback fakes — no device, portal, or credentials
needed. `tests/fixtures/local-protocol.json` doubles as the
language-neutral protocol contract for the planned Node.js port.

MIT license.
