# agora-device-simulator

Simulate hundreds of Agora devices from a single process for scale, fan-out, and
end-to-end testing of the [agora-cms](https://github.com/sslivins/agora-cms)
backend — without needing real Raspberry Pi hardware.

The simulator runs the **real** `cms_client` from the
[sslivins/agora](https://github.com/sslivins/agora) repo (pinned as a git
submodule) with a tiny set of host-specific modules (`shared.board`,
`shared.identity`, systemd/hardware probes) swapped out for in-process shims.
Everything else — the WebSocket protocol, auth handshake, schedule evaluation,
asset fetching (real HTTPS download + SHA-256 verification), eviction, and
state-machine — is the exact same code that runs on a real device.

## Quickstart

```bash
git clone https://github.com/sslivins/agora-device-simulator
cd agora-device-simulator
git submodule update --init --recursive

python -m venv .venv
. .venv/Scripts/activate   # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -e .

# Spin up 10 devices pointed at a local CMS
python -m sim \
    --count 10 \
    --cms-url ws://localhost:8080/ws/device \
    --serial-prefix sim \
    --persist-root C:\Users\me\agora-sim-data
```

All 10 devices will connect to the CMS, register, receive auth tokens, and
appear as **PENDING** in the CMS dashboard ready to be adopted.

## How it works

```
 ┌────────────────── sim process ──────────────────┐
 │                                                 │
 │   DeviceInstance #0 ──┐                         │
 │   DeviceInstance #1 ──┼─► cms_client (real)     │
 │        ...            │     │                   │
 │   DeviceInstance #N ──┘     ▼                   │
 │                        FakePlayer (shim)        │
 │                             │                   │
 │   sys.modules swaps:        ▼                   │
 │     shared.board        desired.json ──► CMS ws │
 │     shared.identity     current.json ◄─ CMS ws  │
 │     cms_client.service                          │
 │       probe functions                           │
 └─────────────────────────────────────────────────┘
```

Each simulated device:
- Gets its own `agora_base` directory (`<persist-root>/<serial>/`) so
  `desired.json`, `current.json`, `auth_token`, and downloaded assets are
  fully isolated.
- Runs `cms_client.service.CMSClient` in its own asyncio task. A
  `ContextVar` carries the per-device profile so shims return the right
  serial / codec list / CPU temp for the task that's calling them.
- Runs a `FakePlayer` that mirrors `DesiredState` → `CurrentState` and
  simulates `loop_count` playback by writing `mode=splash` after
  `fake_asset_duration_sec × N` seconds so the service thinks playback ended.

## CLI options

| Flag | Default | Purpose |
|---|---|---|
| `--count` | `1` | How many devices to spawn |
| `--cms-url` | (required) | WebSocket URL of the CMS, e.g. `ws://localhost:8080/ws/device` |
| `--serial-prefix` | `sim` | Device IDs are `<prefix>-<NNNNN>` |
| `--persist-root` | `$TMP/agora-sim` | Where per-device state directories go |
| `--ramp-rate` | `10` | Max devices started per second (avoid thundering the CMS) |
| `--asset-budget-mb` | `200` | Per-device asset cache budget |
| `--fake-asset-duration-sec` | `10` | Simulated playback duration for loop-count schedules |
| `--keep-state` | `false` | Don't delete per-device state on exit |
| `--control-host` | `127.0.0.1` | Bind address for the HTTP fault-injection API |
| `--control-port` | `9090` | Port for the fault-injection API (`0` disables) |

## Fault injection (control plane)

Each sim process exposes an HTTP API for mutating per-device state at runtime —
so you can exercise CMS alert rules (overheating, low storage, offline, etc.)
without real hardware.

```bash
# See every device's current state
curl http://127.0.0.1:9090/devices

# Jack up a device's CPU temp (CMS overheat alert fires)
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -H 'Content-Type: application/json' \
     -d '{"cpu_temp": 88.5}'

# Simulate a full disk
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"storage_mb_free": 50}'

# Force a device offline for 5 minutes (closes WS, blocks reconnect)
curl -X POST http://127.0.0.1:9090/devices/sim-00007/offline \
     -d '{"duration_sec": 300}'

# Fail the next 3 asset downloads
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"asset_fetch_fail_count": 3}'

# Stop sending heartbeats (socket stays open — tests stale-device detection)
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"heartbeat_stalled": true}'

# Simulate the HDMI display being unplugged (CMS display-disconnect alert
# fires after the configured grace period — default 120s)
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"display_connected": false}'

# ...and plugged back in
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"display_connected": true}'

# Override the reported HDMI port list (defaults to ["HDMI-A-1"])
curl -X POST http://127.0.0.1:9090/devices/sim-00007/fault \
     -d '{"display_ports": ["HDMI-A-1", "HDMI-A-2"]}'

# Clear all faults on a device
curl -X DELETE http://127.0.0.1:9090/devices/sim-00007/fault

# Fleet-wide: take everyone offline (network partition sim)
curl -X POST http://127.0.0.1:9090/fleet/offline -d '{"duration_sec": 60}'

# Fleet-wide: apply a fault to every device
curl -X POST http://127.0.0.1:9090/fleet/fault -d '{"cpu_temp": 85}'
```

| Route | Purpose |
|---|---|
| `GET  /devices` | List all devices + current fault state |
| `GET  /devices/{serial}` | Single device state |
| `POST /devices/{serial}/fault` | Merge fault dict (partial updates allowed) |
| `DELETE /devices/{serial}/fault` | Clear all faults on a device |
| `POST /devices/{serial}/offline` | `{duration_sec}` — force offline |
| `GET  /devices/{serial}/recording` | Inbound WS commands + counters + last config values |
| `DELETE /devices/{serial}/recording` | Clear recorded commands + counters (per-test isolation) |
| `GET  /devices/{serial}/now-playing` | Current playback state (asset, loops, started_at) |
| `POST /fleet/offline` | Same, but all devices |
| `POST /fleet/fault` | Broadcast a fault dict to all devices |

### Inspection example (used by the nightly E2E suite)

```bash
# Reset recording before the UI action
curl -X DELETE http://127.0.0.1:9090/devices/sim-00007/recording

# Playwright clicks "Reboot" in the CMS UI, which triggers a WS message...

# Assert the device received a reboot command
curl http://127.0.0.1:9090/devices/sim-00007/recording
# -> {"count": 1, "counters": {"reboot": 1}, "last_config": {}, "commands": [{"type": "reboot", ...}]}
```

## Status

**v1 (this repo) — in progress**:
- [x] Hardware/probe shims, ContextVar-based profile isolation
- [x] Real asset download + SHA-256 verification (reused from `cms_client`)
- [x] Multi-instance launcher with ramp-rate
- [x] Runtime fault injection HTTP API (#1)
- [ ] YAML scenario loader
- [ ] Virtual clock / fast-forward ("run a week in an hour")

See [agora#251](https://github.com/sslivins/agora/issues/251) for the full
design discussion.
