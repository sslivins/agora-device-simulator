"""Replacement probes for `cms_client.service` module-level helpers."""

from __future__ import annotations

from pathlib import Path

from sim.shims.profile import current_profile


def get_storage_mb(path: Path) -> tuple[int, int]:
    p = current_profile()
    if p.fault.storage_mb_free is not None:
        # storage_mb_free is the amount free — the real probe returns (total, used).
        used = max(p.storage_total_mb - p.fault.storage_mb_free, 0)
        return p.storage_total_mb, used
    return p.storage_total_mb, p.storage_used_mb


def get_cpu_temp() -> float | None:
    p = current_profile()
    if p.fault.cpu_temp is not None:
        return p.fault.cpu_temp
    return p.cpu_temp_c


def is_ssh_enabled() -> bool | None:
    return current_profile().ssh_enabled


def get_local_ip() -> str:
    return current_profile().local_ip


def get_device_id() -> str:
    return current_profile().serial


def get_device_type() -> str:
    return current_profile().model_string


def apply_timezone(tz_name: str) -> None:
    return None
