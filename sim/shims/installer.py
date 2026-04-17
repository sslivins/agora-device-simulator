"""Install the simulator shims into the agora import namespace.

Must be called exactly once, BEFORE importing `cms_client.service` or any
module that pulls in `shared.board` / `shared.identity`. Typically called
from `sim/__main__.py` after setting up sys.path to include the agora
submodule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

_APPLIED = False
logger = logging.getLogger("agora_sim.shims")


def apply_shims() -> None:
    global _APPLIED
    if _APPLIED:
        return

    from sim.shims import board as shim_board
    from sim.shims import identity as shim_identity

    sys.modules["shared.board"] = shim_board
    sys.modules["shared.identity"] = shim_identity

    from cms_client import service as cms_service
    from sim.shims import probes
    from sim.shims.profile import current_profile

    cms_service._get_storage_mb = probes.get_storage_mb
    cms_service._get_cpu_temp = probes.get_cpu_temp
    cms_service._is_ssh_enabled = probes.is_ssh_enabled
    cms_service._get_local_ip = probes.get_local_ip
    cms_service._get_device_id = probes.get_device_id
    cms_service._get_device_type = probes.get_device_type
    cms_service.CMSClient._apply_timezone = lambda self, tz_name: None

    _install_fault_hooks(cms_service, current_profile)

    _APPLIED = True


def _install_fault_hooks(cms_service, current_profile) -> None:
    """Wrap CMSClient methods so runtime faults can steer behaviour."""
    CMSClient = cms_service.CMSClient

    # ---- offline: block (re)connect until fault deadline passes ----
    _orig_connect_and_run = CMSClient._connect_and_run

    async def _connect_and_run_faulted(self):
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.offline_until is not None:
            loop = asyncio.get_event_loop()
            remaining = fault.offline_until - loop.time()
            if remaining > 0:
                logger.info("[%s] offline fault: sleeping %.1fs before reconnect",
                            self.device_id, remaining)
                await asyncio.sleep(remaining)
            fault.offline_until = None
        return await _orig_connect_and_run(self)

    CMSClient._connect_and_run = _connect_and_run_faulted

    # ---- heartbeat stall: skip sending status messages while active ----
    _orig_send_status = CMSClient._send_status

    async def _send_status_faulted(self):
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.heartbeat_stalled:
            return  # drop the heartbeat silently
        return await _orig_send_status(self)

    CMSClient._send_status = _send_status_faulted

    # ---- asset fetch failure: synthesize a fetch_failed response ----
    _orig_handle_fetch_asset = CMSClient._handle_fetch_asset

    async def _handle_fetch_asset_faulted(self, msg: dict, ws) -> None:
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.asset_fetch_fail_count > 0:
            fault.asset_fetch_fail_count -= 1
            asset_name = msg.get("asset_name", "")
            logger.info("[%s] injected asset_fetch_fail for %s",
                        self.device_id, asset_name)
            fail = {
                "type": "fetch_failed",
                "protocol_version": cms_service.PROTOCOL_VERSION,
                "device_id": self.device_id,
                "asset": asset_name,
                "reason": "simulated_fault",
            }
            try:
                await ws.send(json.dumps(fail))
            except Exception:
                logger.debug("Could not send synthesized fetch_failed", exc_info=True)
            return
        return await _orig_handle_fetch_asset(self, msg, ws)

    CMSClient._handle_fetch_asset = _handle_fetch_asset_faulted
