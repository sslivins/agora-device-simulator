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

## Status

**v1 (this repo) — in progress**:
- [x] Hardware/probe shims, ContextVar-based profile isolation
- [x] Real asset download + SHA-256 verification (reused from `cms_client`)
- [x] Multi-instance launcher with ramp-rate
- [ ] YAML scenario loader
- [ ] Virtual clock / fast-forward ("run a week in an hour")

See [agora#251](https://github.com/sslivins/agora/issues/251) for the full
design discussion.
