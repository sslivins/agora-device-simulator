"""Install the simulator shims into the agora import namespace.

Must be called exactly once, BEFORE importing `cms_client.service` or any
module that pulls in `shared.board` / `shared.identity`. Typically called
from `sim/__main__.py` after setting up sys.path to include the agora
submodule.
"""

from __future__ import annotations

import sys

_APPLIED = False


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

    cms_service._get_storage_mb = probes.get_storage_mb
    cms_service._get_cpu_temp = probes.get_cpu_temp
    cms_service._is_ssh_enabled = probes.is_ssh_enabled
    cms_service._get_local_ip = probes.get_local_ip
    cms_service._get_device_id = probes.get_device_id
    cms_service._get_device_type = probes.get_device_type
    cms_service.CMSClient._apply_timezone = lambda self, tz_name: None

    _APPLIED = True
