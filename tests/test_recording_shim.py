"""Verify the recording shim wraps every CMSClient._handle_* method."""

from __future__ import annotations

import types
from typing import Any

import pytest

from sim.shims.installer import _RECORDED_HANDLERS, _install_recording_hooks
from sim.shims.profile import DeviceProfile, set_profile


def _build_stub_cms_service():
    """Create a minimal cms_service module with a CMSClient that has the handlers."""
    mod = types.ModuleType("stub_cms_service")

    class CMSClient:
        def __init__(self):
            self.calls: list[tuple[str, Any]] = []

        async def _handle_auth_assigned(self, msg):
            self.calls.append(("_handle_auth_assigned", msg))

        async def _handle_sync(self, msg):
            self.calls.append(("_handle_sync", msg))

        async def _handle_play(self, msg):
            self.calls.append(("_handle_play", msg))

        async def _handle_stop(self):
            self.calls.append(("_handle_stop", None))

        async def _handle_fetch_asset(self, msg, ws):
            self.calls.append(("_handle_fetch_asset", msg))

        async def _handle_delete_asset(self, msg, ws):
            self.calls.append(("_handle_delete_asset", msg))

        async def _handle_config(self, msg):
            self.calls.append(("_handle_config", msg))

        async def _handle_reboot(self, ws):
            self.calls.append(("_handle_reboot", None))

        async def _handle_upgrade(self, ws):
            self.calls.append(("_handle_upgrade", None))

        async def _handle_factory_reset(self, ws):
            self.calls.append(("_handle_factory_reset", None))

        async def _handle_wipe_assets(self, msg, ws):
            self.calls.append(("_handle_wipe_assets", msg))

        async def _handle_request_logs(self, msg, ws):
            self.calls.append(("_handle_request_logs", msg))

    mod.CMSClient = CMSClient
    return mod


@pytest.fixture
def recorded_client():
    from sim.shims.profile import current_profile

    mod = _build_stub_cms_service()
    _install_recording_hooks(mod, current_profile)
    profile = DeviceProfile(serial="SIM-HOOK-1")
    token = set_profile(profile)  # noqa: F841 - keeps the token alive in this scope
    try:
        yield mod.CMSClient(), profile
    finally:
        # ContextVar reset is optional in test scope; pytest gives us a fresh context
        pass


async def test_every_handler_records_its_type(recorded_client):
    client, profile = recorded_client

    await client._handle_auth_assigned({"token": "x"})
    await client._handle_sync({"sync": True})
    await client._handle_play({"asset": "p.png"})
    await client._handle_stop()
    await client._handle_fetch_asset({"asset_name": "a"}, ws=None)
    await client._handle_delete_asset({"asset_name": "a"}, ws=None)
    await client._handle_config({"ssh_enabled": True})
    await client._handle_reboot(ws=None)
    await client._handle_upgrade(ws=None)
    await client._handle_factory_reset(ws=None)
    await client._handle_wipe_assets({"reason": "adopted"}, ws=None)
    await client._handle_request_logs({"request_id": "r1"}, ws=None)

    recorded_types = [c["type"] for c in profile.recorder.commands]
    expected_types = [t for _, t, _ in _RECORDED_HANDLERS]
    assert recorded_types == expected_types
    assert profile.recorder.counters["reboot"] == 1
    assert profile.recorder.counters["config"] == 1
    assert profile.recorder.last_config == {"ssh_enabled": True}


async def test_handler_still_called_after_recording(recorded_client):
    client, _ = recorded_client
    await client._handle_reboot(ws=None)
    await client._handle_config({"ssh_enabled": False})
    assert ("_handle_reboot", None) in client.calls
    assert ("_handle_config", {"ssh_enabled": False}) in client.calls


async def test_no_profile_bound_is_noop():
    """If no DeviceProfile is in context, recording silently skips."""
    import asyncio

    mod = _build_stub_cms_service()
    from sim.shims.profile import current_profile

    _install_recording_hooks(mod, current_profile)

    async def run_in_clean_context():
        # Fresh context inherits from caller; explicitly clear the profile here.
        from sim.shims.profile import _CURRENT_PROFILE
        _CURRENT_PROFILE.set(None)
        client = mod.CMSClient()
        # Must not raise despite no bound profile
        await client._handle_reboot(ws=None)
        await client._handle_config({"ssh_enabled": True})
        return client

    import contextvars
    ctx = contextvars.copy_context()
    fut = asyncio.get_event_loop().create_task(run_in_clean_context(), context=ctx)
    client = await fut
    assert ("_handle_reboot", None) in client.calls
