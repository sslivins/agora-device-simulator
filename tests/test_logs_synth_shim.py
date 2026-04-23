"""Verify the logs_synth shim intercepts journalctl + counts branches."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from typing import Any

import pytest

from sim.shims.installer import (
    _install_logs_branch_counters,
    _install_logs_synth_hook,
    _synth_bytes,
)
from sim.shims.profile import DeviceProfile, current_profile, set_profile


# ── unit: _synth_bytes ───────────────────────────────────────────────────


def test_synth_bytes_int_produces_exact_size():
    out = _synth_bytes("agora-player", 4096)
    assert len(out.encode("utf-8")) == 4096


def test_synth_bytes_zero_is_empty():
    assert _synth_bytes("svc", 0) == ""


def test_synth_bytes_str_passthrough():
    assert _synth_bytes("svc", "hello") == "hello"


def test_synth_bytes_rejects_bool():
    with pytest.raises(TypeError):
        _synth_bytes("svc", True)


def test_synth_bytes_rejects_negative():
    with pytest.raises(ValueError):
        _synth_bytes("svc", -1)


def test_synth_bytes_rejects_oversize():
    with pytest.raises(ValueError):
        _synth_bytes("svc", 26 * 1024 * 1024)


# ── stub helpers for the firmware module shape ──────────────────────────


def _build_stub_cms_service(real_subprocess: types.ModuleType):
    """Build a stub cms_service module exposing the names the hooks read."""
    mod = types.ModuleType("stub_cms_service_logs")
    mod.subprocess = real_subprocess  # the hook patches mod.subprocess.run
    mod.PROTOCOL_VERSION = 2
    mod.LOGS_JSON_MAX_BYTES = 900_000

    class CMSClient:
        def __init__(self):
            self.device_id = "STUB-1"
            self.uploaded: list[tuple[str, bytes]] = []

        async def _upload_logs_bundle(self, request_id, tar_gz):
            self.uploaded.append((request_id, tar_gz))

        async def _handle_request_logs(self, msg, ws):
            # Mimics firmware: shells out per service, then no-ops.
            for s in (msg.get("services") or []):
                mod.subprocess.run(
                    ["journalctl", "-u", s, "--since=1h ago"],
                    capture_output=True, text=True, timeout=30,
                )

    mod.CMSClient = CMSClient
    return mod


# ── logs_synth subprocess hook ──────────────────────────────────────────


def test_logs_synth_intercepts_journalctl():
    # Use a fresh subprocess module copy so test doesn't mutate global.
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)

    profile = DeviceProfile(serial="STUB-1")
    profile.logs_synth = {"agora-player": 256}
    token = set_profile(profile)
    try:
        result = cms.subprocess.run(
            ["journalctl", "-u", "agora-player", "--since=1h ago"],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.reset(token)

    assert result.returncode == 0
    assert len(result.stdout.encode("utf-8")) == 256
    assert "[agora-player]" in result.stdout


def test_logs_synth_passes_through_when_no_profile():
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)

    # No profile bound — should fall through to real subprocess.run, which
    # will FileNotFoundError on `journalctl` in the test environment.
    with pytest.raises(FileNotFoundError):
        cms.subprocess.run(
            ["journalctl", "-u", "x"],
            capture_output=True, text=True, timeout=5,
        )


def test_logs_synth_passes_through_non_journalctl():
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)

    profile = DeviceProfile(serial="STUB-1")
    profile.logs_synth = {"x": 100}
    token = set_profile(profile)
    try:
        # Call something that's NOT journalctl. On Windows this'll bomb;
        # we only care that we *attempted* the real call (i.e. didn't
        # silently return CompletedProcess).
        with pytest.raises(Exception):
            cms.subprocess.run(
                ["this-binary-does-not-exist-xyz"],
                capture_output=True, timeout=2,
            )
    finally:
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.reset(token)


def test_logs_synth_unconfigured_service_returns_empty():
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)

    profile = DeviceProfile(serial="STUB-1")
    profile.logs_synth = {"agora-player": 100}
    token = set_profile(profile)
    try:
        result = cms.subprocess.run(
            ["journalctl", "-u", "other-service"],
            capture_output=True, text=True,
        )
    finally:
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.reset(token)

    assert result.returncode == 0
    assert result.stdout == ""


# ── branch counters ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_branch_counter_logs_ws_json_below_threshold():
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)
    _install_logs_branch_counters(cms, current_profile)

    profile = DeviceProfile(serial="STUB-1")
    profile.logs_synth = {"svc": 1024}  # well under 900k
    token = set_profile(profile)
    try:
        client = cms.CMSClient()
        await client._handle_request_logs(
            {"services": ["svc"], "request_id": "r1"}, ws=None,
        )
    finally:
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.reset(token)

    assert profile.recorder.counters.get("logs_ws_json") == 1
    assert "logs_upload" not in profile.recorder.counters


@pytest.mark.asyncio
async def test_branch_counter_logs_upload_above_threshold():
    sp = types.ModuleType("sp")
    sp.run = subprocess.run
    sp.CompletedProcess = subprocess.CompletedProcess
    cms = _build_stub_cms_service(sp)
    _install_logs_synth_hook(cms, current_profile)
    _install_logs_branch_counters(cms, current_profile)

    profile = DeviceProfile(serial="STUB-1")
    profile.logs_synth = {"svc": 950_000}  # exceeds 900k cap
    token = set_profile(profile)
    try:
        client = cms.CMSClient()
        # Stub _handle_request_logs doesn't itself call _upload, so call
        # it directly to prove the counter wraps it correctly.
        await client._upload_logs_bundle("r1", b"fake-tarball")
        await client._handle_request_logs(
            {"services": ["svc"], "request_id": "r1"}, ws=None,
        )
    finally:
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.reset(token)

    assert profile.recorder.counters.get("logs_upload") == 1
    # JSON branch counter must NOT fire when synth payload exceeds threshold.
    assert "logs_ws_json" not in profile.recorder.counters
