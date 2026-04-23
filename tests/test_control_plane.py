"""Integration tests for the control-plane recording + now-playing endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sim import control
from sim.instance import INSTANCES
from sim.shims.profile import DeviceProfile


@dataclass
class _FakePlayer:
    _current_asset: str | None = None
    _current_loop_count: int | None = None
    _loops_done: int = 0
    _play_started_at: datetime | None = None


class _FakeInstance:
    def __init__(self, serial: str):
        self.profile = DeviceProfile(serial=serial)
        self.cms_url = "http://test"
        self._client = None
        self._fake_player = _FakePlayer()


@pytest.fixture
async def client():
    inst = _FakeInstance("SIM-REC-1")
    INSTANCES["SIM-REC-1"] = inst
    try:
        app = control.build_app()
        async with TestClient(TestServer(app)) as c:
            yield c, inst
    finally:
        INSTANCES.pop("SIM-REC-1", None)


async def test_index_lists_new_routes(client):
    c, _ = client
    resp = await c.get("/")
    body = await resp.json()
    assert "GET    /devices/{serial}/recording" in body["routes"]
    assert "DELETE /devices/{serial}/recording" in body["routes"]
    assert "GET    /devices/{serial}/now-playing" in body["routes"]


async def test_device_snapshot_includes_recording_and_now_playing(client):
    c, inst = client
    inst.profile.recorder.record("reboot", {"type": "reboot"})
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["recording"]["count"] == 1
    assert body["recording"]["counters"]["reboot"] == 1
    assert body["now_playing"] is None


async def test_now_playing_reflects_fake_player(client):
    c, inst = client
    inst._fake_player._current_asset = "poster.png"
    inst._fake_player._current_loop_count = 3
    inst._fake_player._loops_done = 1
    inst._fake_player._play_started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resp = await c.get("/devices/SIM-REC-1/now-playing")
    body = await resp.json()
    assert body["now_playing"]["asset"] == "poster.png"
    assert body["now_playing"]["loop_count"] == 3
    assert body["now_playing"]["loops_done"] == 1
    assert body["now_playing"]["started_at"].startswith("2026-01-01")


async def test_get_recording_returns_commands_and_counters(client):
    c, inst = client
    inst.profile.recorder.record("reboot", {"type": "reboot"})
    inst.profile.recorder.record("config", {"ssh_enabled": True})
    resp = await c.get("/devices/SIM-REC-1/recording")
    body = await resp.json()
    assert body["count"] == 2
    assert body["counters"] == {"reboot": 1, "config": 1}
    assert body["last_config"] == {"ssh_enabled": True}
    assert [x["type"] for x in body["commands"]] == ["reboot", "config"]


async def test_delete_recording_clears(client):
    c, inst = client
    inst.profile.recorder.record("reboot", {})
    resp = await c.delete("/devices/SIM-REC-1/recording")
    body = await resp.json()
    assert body["count"] == 0
    assert inst.profile.recorder.counters == {}


async def test_recording_404_on_unknown_device(client):
    c, _ = client
    resp = await c.get("/devices/NOPE/recording")
    assert resp.status == 404


# ── display fault injection (display_connected / display_ports) ────────────


async def test_set_display_disconnected_fault(client):
    c, inst = client
    resp = await c.post(
        "/devices/SIM-REC-1/fault", json={"display_connected": False}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["fault"]["display_connected"] is False
    assert inst.profile.fault.display_connected is False


async def test_set_display_ports_fault(client):
    c, inst = client
    resp = await c.post(
        "/devices/SIM-REC-1/fault",
        json={"display_ports": ["HDMI-A-1", "HDMI-A-2"]},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["fault"]["display_ports"] == ["HDMI-A-1", "HDMI-A-2"]
    assert inst.profile.fault.display_ports == ["HDMI-A-1", "HDMI-A-2"]


async def test_clear_resets_display_fields(client):
    c, inst = client
    inst.profile.fault.display_connected = False
    inst.profile.fault.display_ports = ["HDMI-A-1"]
    resp = await c.delete("/devices/SIM-REC-1/fault")
    body = await resp.json()
    assert body["fault"]["display_connected"] is None
    assert body["fault"]["display_ports"] is None
    assert inst.profile.fault.display_connected is None
    assert inst.profile.fault.display_ports is None


async def test_display_ports_must_be_list_of_strings(client):
    c, _ = client
    resp = await c.post(
        "/devices/SIM-REC-1/fault", json={"display_ports": "HDMI-A-1"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert "display_ports" in body["detail"]


async def test_snapshot_exposes_display_fault_fields(client):
    c, inst = client
    inst.profile.fault.display_connected = False
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert "display_connected" in body["fault"]
    assert "display_ports" in body["fault"]
    assert body["fault"]["display_connected"] is False


class _FakeWsState:
    def __init__(self, name: str):
        self.name = name


class _FakeRawWs:
    """Pre-#127 shape: raw websockets library object with .state enum."""
    def __init__(self, state_name: str = "OPEN"):
        self.state = _FakeWsState(state_name)


class _FakeTransport:
    """Post-#127 shape: wrapper with _closed bool and inner _ws."""
    def __init__(self, closed: bool = False, inner_state: str = "OPEN"):
        self._closed = closed
        self._ws = _FakeRawWs(inner_state)


class _FakeClient:
    def __init__(self, ws):
        self._ws = ws


async def test_ws_open_reads_raw_websocket_state(client):
    c, inst = client
    inst._client = _FakeClient(_FakeRawWs("OPEN"))
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["ws_open"] is True


async def test_ws_open_raw_websocket_closed(client):
    c, inst = client
    inst._client = _FakeClient(_FakeRawWs("CLOSED"))
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["ws_open"] is False


async def test_ws_open_unwraps_transport_wrapper(client):
    """Regression: since agora#127 _client._ws is a transport wrapper.

    The snapshot must unwrap it to reach the underlying websocket's .state,
    otherwise ws_open is always False and smoke tests wedge.
    """
    c, inst = client
    inst._client = _FakeClient(_FakeTransport(closed=False, inner_state="OPEN"))
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["ws_open"] is True


async def test_ws_open_transport_wrapper_closed(client):
    c, inst = client
    inst._client = _FakeClient(_FakeTransport(closed=True, inner_state="OPEN"))
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["ws_open"] is False


async def test_ws_open_transport_wrapper_inner_closed(client):
    c, inst = client
    inst._client = _FakeClient(_FakeTransport(closed=False, inner_state="CLOSED"))
    resp = await c.get("/devices/SIM-REC-1")
    body = await resp.json()
    assert body["ws_open"] is False


# ── logs_synth control plane ────────────────────────────────────────────


async def test_set_logs_synth_accepts_int_and_str(client):
    c, inst = client
    resp = await c.post(
        "/devices/SIM-REC-1/logs",
        json={"agora-player": 4096, "agora-api": "literal-text"},
    )
    assert resp.status == 200
    body = await resp.json()
    # Response should NOT echo the literal payload back (avoids huge bodies);
    # only a compact summary.
    assert body["serial"] == "SIM-REC-1"
    summary = body["logs_synth"]
    assert summary["agora-player"] == ["int", 4096]
    assert summary["agora-api"] == ["str", len("literal-text")]
    # Profile actually mutated.
    assert inst.profile.logs_synth == {
        "agora-player": 4096, "agora-api": "literal-text",
    }


async def test_set_logs_synth_rejects_bool(client):
    c, _ = client
    resp = await c.post("/devices/SIM-REC-1/logs", json={"svc": True})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_field"
    assert "bool" in body["detail"].lower()


async def test_set_logs_synth_rejects_negative(client):
    c, _ = client
    resp = await c.post("/devices/SIM-REC-1/logs", json={"svc": -1})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_field"


async def test_set_logs_synth_rejects_oversize(client):
    c, _ = client
    resp = await c.post(
        "/devices/SIM-REC-1/logs", json={"svc": 26 * 1024 * 1024},
    )
    assert resp.status == 400


async def test_set_logs_synth_rejects_empty_body(client):
    c, _ = client
    resp = await c.post("/devices/SIM-REC-1/logs", json={})
    assert resp.status == 400


async def test_clear_logs_synth(client):
    c, inst = client
    inst.profile.logs_synth = {"svc": 1024}
    resp = await c.delete("/devices/SIM-REC-1/logs")
    assert resp.status == 200
    body = await resp.json()
    assert body["logs_synth"] is None
    assert inst.profile.logs_synth is None


async def test_logs_synth_unknown_serial(client):
    c, _ = client
    resp = await c.post("/devices/UNKNOWN/logs", json={"svc": 1024})
    assert resp.status == 404


async def test_index_lists_logs_routes(client):
    c, _ = client
    resp = await c.get("/")
    body = await resp.json()
    assert "POST   /devices/{serial}/logs" in body["routes"]
    assert "DELETE /devices/{serial}/logs" in body["routes"]

