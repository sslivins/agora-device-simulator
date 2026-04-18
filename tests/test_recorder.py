"""Unit tests for CommandRecorder."""

from __future__ import annotations

from sim.shims.profile import RECORDER_MAX_COMMANDS, CommandRecorder


def test_records_type_and_payload_and_ts():
    r = CommandRecorder()
    r.record("reboot", {"type": "reboot"})
    assert len(r.commands) == 1
    entry = r.commands[0]
    assert entry["type"] == "reboot"
    assert entry["payload"] == {"type": "reboot"}
    assert isinstance(entry["ts"], float)


def test_counters_increment_per_type():
    r = CommandRecorder()
    r.record("reboot", {})
    r.record("reboot", {})
    r.record("upgrade", {})
    assert r.counters == {"reboot": 2, "upgrade": 1}


def test_config_last_values_tracked():
    r = CommandRecorder()
    r.record("config", {"type": "config", "ssh_enabled": True})
    r.record("config", {"type": "config", "ssh_enabled": False, "local_api_enabled": True})
    assert r.last_config == {"ssh_enabled": False, "local_api_enabled": True}


def test_ring_buffer_caps_at_max():
    r = CommandRecorder()
    for i in range(RECORDER_MAX_COMMANDS + 25):
        r.record("sync", {"i": i})
    assert len(r.commands) == RECORDER_MAX_COMMANDS
    assert r.commands[0]["payload"]["i"] == 25
    assert r.commands[-1]["payload"]["i"] == RECORDER_MAX_COMMANDS + 24
    assert r.counters["sync"] == RECORDER_MAX_COMMANDS + 25


def test_reset_clears_everything():
    r = CommandRecorder()
    r.record("reboot", {})
    r.record("config", {"ssh_enabled": True})
    r.reset()
    assert len(r.commands) == 0
    assert r.counters == {}
    assert r.last_config == {}


def test_to_dict_shape():
    r = CommandRecorder()
    r.record("reboot", {"type": "reboot"})
    d = r.to_dict()
    assert d["count"] == 1
    assert d["counters"] == {"reboot": 1}
    assert d["last_config"] == {}
    assert d["commands"][0]["type"] == "reboot"
