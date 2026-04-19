"""Tests for display fault injection through FakePlayer → current.json.

Validates that toggling ``profile.fault.display_connected`` /
``profile.fault.display_ports`` causes the FakePlayer to re-emit
``current.json`` so the cms_client's heartbeat loop forwards the new values
to the CMS, exercising the display-disconnect alert path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sim.fake_player import FakePlayer
from sim.shims.profile import DeviceProfile


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "desired.json", tmp_path / "current.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.asyncio
async def test_default_display_state_is_connected(paths):
    desired, current = paths
    profile = DeviceProfile(serial="sim-disp-1")
    player = FakePlayer(desired, current, profile=profile)

    task = asyncio.create_task(player.run())
    try:
        # initial write happens immediately on run(); allow scheduler tick.
        for _ in range(20):
            if current.exists():
                break
            await asyncio.sleep(0.05)
        body = _read(current)
        assert body["display_connected"] is True
        assert body["display_ports"] == ["HDMI-A-1"]
    finally:
        player.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_display_disconnect_fault_is_reflected(paths):
    desired, current = paths
    profile = DeviceProfile(serial="sim-disp-2")
    player = FakePlayer(desired, current, profile=profile)

    task = asyncio.create_task(player.run())
    try:
        # Wait for first write
        for _ in range(20):
            if current.exists():
                break
            await asyncio.sleep(0.05)
        assert _read(current)["display_connected"] is True

        # Inject the fault — next poll tick (~1s) should re-emit.
        profile.fault.display_connected = False
        profile.fault.display_ports = []
        for _ in range(30):
            await asyncio.sleep(0.1)
            body = _read(current)
            if body["display_connected"] is False and body["display_ports"] == []:
                break
        assert body["display_connected"] is False, body
        assert body["display_ports"] == [], body

        # Clear the fault; player should restore defaults on the next tick.
        profile.fault.display_connected = None
        profile.fault.display_ports = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            body = _read(current)
            if body["display_connected"] is True:
                break
        assert body["display_connected"] is True, body
        assert body["display_ports"] == ["HDMI-A-1"], body
    finally:
        player.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_display_ports_override(paths):
    desired, current = paths
    profile = DeviceProfile(serial="sim-disp-3")
    profile.fault.display_ports = ["HDMI-A-1", "HDMI-A-2"]
    player = FakePlayer(desired, current, profile=profile)

    task = asyncio.create_task(player.run())
    try:
        for _ in range(20):
            if current.exists():
                break
            await asyncio.sleep(0.05)
        body = _read(current)
        assert body["display_ports"] == ["HDMI-A-1", "HDMI-A-2"]
        # Default display_connected still true since we only set ports.
        assert body["display_connected"] is True
    finally:
        player.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
