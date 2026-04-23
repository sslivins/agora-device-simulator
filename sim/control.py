"""HTTP control plane for runtime fault injection.

Exposes a small aiohttp server (default 127.0.0.1:9090) that mutates
per-device FaultState. See the README or `GET /` for the full API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from sim.instance import INSTANCES

logger = logging.getLogger("agora_sim.control")


def _now_playing(inst) -> dict | None:
    """Read current playback state from the FakePlayer, if any."""
    fp = getattr(inst, "_fake_player", None)
    if fp is None:
        return None
    asset = getattr(fp, "_current_asset", None)
    if not asset:
        return None
    started = getattr(fp, "_play_started_at", None)
    return {
        "asset": asset,
        "loop_count": getattr(fp, "_current_loop_count", None),
        "loops_done": getattr(fp, "_loops_done", 0),
        "started_at": started.isoformat() if started is not None else None,
    }


def _device_snapshot(serial: str, inst) -> dict:
    loop = asyncio.get_event_loop()
    fault_dict = inst.profile.fault.to_dict()
    # Translate offline_until (monotonic loop time) into remaining seconds
    if fault_dict["offline_until"] is not None:
        remaining = fault_dict["offline_until"] - loop.time()
        fault_dict["offline_remaining_sec"] = max(round(remaining, 1), 0.0)
    else:
        fault_dict["offline_remaining_sec"] = 0.0
    ws_open = False
    try:
        ws = getattr(inst._client, "_ws", None)
        if ws is not None:
            # Since agora#127, inst._client._ws is a transport wrapper
            # (DirectTransport / WPSTransport) with an internal _closed flag
            # and the raw websocket stored on ._ws. Unwrap it so ws_open
            # reflects the underlying connection state rather than always
            # being False (the wrapper has no .state/.open/.closed).
            if hasattr(ws, "_closed") and hasattr(ws, "_ws"):
                if ws._closed:
                    ws_open = False
                else:
                    inner = ws._ws
                    state = getattr(inner, "state", None)
                    if state is not None:
                        ws_open = getattr(state, "name", "") == "OPEN"
                    elif hasattr(inner, "open"):
                        ws_open = bool(inner.open)
                    elif hasattr(inner, "closed"):
                        ws_open = not bool(inner.closed)
                    else:
                        # wrapper not closed and underlying ws shape unknown — trust the wrapper.
                        ws_open = True
            else:
                # Raw websocket (pre-#127 behaviour / direct assignment).
                # websockets lib: .state is a State enum (CONNECTING/OPEN/CLOSING/CLOSED)
                state = getattr(ws, "state", None)
                if state is not None:
                    ws_open = getattr(state, "name", "") == "OPEN"
                elif hasattr(ws, "open"):
                    ws_open = bool(ws.open)
                elif hasattr(ws, "closed"):
                    ws_open = not bool(ws.closed)
    except Exception:
        pass
    return {
        "serial": serial,
        "board": inst.profile.board,
        "cms_url": inst.cms_url,
        "ws_open": ws_open,
        "fault": fault_dict,
        "recording": inst.profile.recorder.to_dict(),
        "now_playing": _now_playing(inst),
    }


async def _list_devices(request: web.Request) -> web.Response:
    return web.json_response({
        "count": len(INSTANCES),
        "devices": [_device_snapshot(s, i) for s, i in sorted(INSTANCES.items())],
    })


async def _get_device(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    return web.json_response(_device_snapshot(serial, inst))


def _apply_fault_dict(fault, body: dict[str, Any]) -> None:
    """Merge a JSON body into a FaultState. Raises ValueError on bad input."""
    if "cpu_temp" in body:
        v = body["cpu_temp"]
        fault.cpu_temp = None if v is None else float(v)
    if "storage_mb_free" in body:
        v = body["storage_mb_free"]
        fault.storage_mb_free = None if v is None else int(v)
    if "codecs" in body:
        v = body["codecs"]
        if v is None:
            fault.codecs = None
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            fault.codecs = list(v)
        else:
            raise ValueError("codecs must be a list of strings or null")
    if "asset_fetch_fail_count" in body:
        fault.asset_fetch_fail_count = int(body["asset_fetch_fail_count"])
    if "heartbeat_stalled" in body:
        fault.heartbeat_stalled = bool(body["heartbeat_stalled"])
    if "display_connected" in body:
        v = body["display_connected"]
        fault.display_connected = None if v is None else bool(v)
    if "display_ports" in body:
        v = body["display_ports"]
        if v is None:
            fault.display_ports = None
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            fault.display_ports = list(v)
        else:
            raise ValueError("display_ports must be a list of strings or null")


async def _set_fault(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    try:
        _apply_fault_dict(inst.profile.fault, body)
    except (ValueError, TypeError) as e:
        return web.json_response({"error": "invalid_field", "detail": str(e)}, status=400)
    logger.info("[%s] fault updated: %s", serial, body)
    return web.json_response(_device_snapshot(serial, inst))


async def _clear_fault(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    inst.profile.fault.clear()
    logger.info("[%s] faults cleared", serial)
    return web.json_response(_device_snapshot(serial, inst))


async def _set_offline(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    duration = float(body.get("duration_sec", 30.0))
    await inst.set_offline(duration)
    logger.info("[%s] forced offline for %.1fs", serial, duration)
    return web.json_response(_device_snapshot(serial, inst))


async def _fleet_offline(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    duration = float(body.get("duration_sec", 30.0))
    count = 0
    for inst in list(INSTANCES.values()):
        await inst.set_offline(duration)
        count += 1
    logger.info("fleet offline: %d devices for %.1fs", count, duration)
    return web.json_response({"affected": count, "duration_sec": duration})


async def _fleet_fault(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    affected = 0
    for inst in list(INSTANCES.values()):
        try:
            _apply_fault_dict(inst.profile.fault, body)
            affected += 1
        except (ValueError, TypeError) as e:
            return web.json_response(
                {"error": "invalid_field", "detail": str(e), "affected_before_error": affected},
                status=400,
            )
    logger.info("fleet fault applied to %d devices: %s", affected, body)
    return web.json_response({"affected": affected, "fault": body})


async def _get_recording(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    return web.json_response(inst.profile.recorder.to_dict())


async def _reset_recording(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    inst.profile.recorder.reset()
    logger.info("[%s] recording reset", serial)
    return web.json_response(inst.profile.recorder.to_dict())


async def _get_now_playing(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    return web.json_response({"serial": serial, "now_playing": _now_playing(inst)})


_LOGS_SYNTH_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB per service hard ceiling


def _validate_logs_synth_body(body: Any) -> dict[str, Any]:
    """Validate and normalize a POST /devices/{serial}/logs payload.

    Returns the normalized dict on success; raises ValueError otherwise.
    Each value must be either ``int >= 0`` (bytes of synthetic content)
    or ``str``. Booleans (which Python treats as int) are rejected.
    """
    if not isinstance(body, dict) or not body:
        raise ValueError("body must be a non-empty JSON object of {service: int|str}")
    out: dict[str, Any] = {}
    for k, v in body.items():
        if not isinstance(k, str) or not k:
            raise ValueError(f"service key must be a non-empty string, got {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise ValueError(
                f"logs_synth[{k!r}] must be int (bytes) or str, got {type(v).__name__}"
            )
        if isinstance(v, int):
            if v < 0:
                raise ValueError(f"logs_synth[{k!r}] must be >= 0, got {v}")
            if v > _LOGS_SYNTH_MAX_BYTES:
                raise ValueError(
                    f"logs_synth[{k!r}] {v} exceeds {_LOGS_SYNTH_MAX_BYTES}-byte cap"
                )
        out[k] = v
    return out


def _logs_synth_summary(synth: dict[str, Any] | None) -> dict | None:
    """Compact summary suitable for control-plane responses.

    Avoids echoing potentially-multi-MB literal log strings back to the
    test runner; just reports per-service mode and size.
    """
    if not synth:
        return None
    return {
        s: ("int", v) if isinstance(v, int) and not isinstance(v, bool)
        else ("str", len(v) if isinstance(v, str) else None)
        for s, v in synth.items()
    }


async def _set_logs_synth(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    try:
        normalized = _validate_logs_synth_body(body)
    except ValueError as e:
        return web.json_response({"error": "invalid_field", "detail": str(e)}, status=400)
    inst.profile.logs_synth = normalized
    logger.info("[%s] logs_synth set: %s", serial, _logs_synth_summary(normalized))
    return web.json_response({
        "serial": serial,
        "logs_synth": _logs_synth_summary(normalized),
    })


async def _clear_logs_synth(request: web.Request) -> web.Response:
    serial = request.match_info["serial"]
    inst = INSTANCES.get(serial)
    if inst is None:
        return web.json_response({"error": "not_found", "serial": serial}, status=404)
    inst.profile.logs_synth = None
    logger.info("[%s] logs_synth cleared", serial)
    return web.json_response({"serial": serial, "logs_synth": None})


async def _index(request: web.Request) -> web.Response:
    return web.json_response({
        "service": "agora-device-simulator control plane",
        "device_count": len(INSTANCES),
        "routes": [
            "GET    /",
            "GET    /devices",
            "GET    /devices/{serial}",
            "POST   /devices/{serial}/fault",
            "DELETE /devices/{serial}/fault",
            "POST   /devices/{serial}/offline",
            "GET    /devices/{serial}/recording",
            "DELETE /devices/{serial}/recording",
            "GET    /devices/{serial}/now-playing",
            "POST   /devices/{serial}/logs",
            "DELETE /devices/{serial}/logs",
            "POST   /fleet/offline",
            "POST   /fleet/fault",
        ],
    })


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/devices", _list_devices)
    app.router.add_get("/devices/{serial}", _get_device)
    app.router.add_post("/devices/{serial}/fault", _set_fault)
    app.router.add_delete("/devices/{serial}/fault", _clear_fault)
    app.router.add_post("/devices/{serial}/offline", _set_offline)
    app.router.add_get("/devices/{serial}/recording", _get_recording)
    app.router.add_delete("/devices/{serial}/recording", _reset_recording)
    app.router.add_get("/devices/{serial}/now-playing", _get_now_playing)
    app.router.add_post("/devices/{serial}/logs", _set_logs_synth)
    app.router.add_delete("/devices/{serial}/logs", _clear_logs_synth)
    app.router.add_post("/fleet/offline", _fleet_offline)
    app.router.add_post("/fleet/fault", _fleet_fault)
    return app


async def start_control_plane(host: str, port: int) -> web.AppRunner:
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Control plane listening on http://%s:%d", host, port)
    return runner
