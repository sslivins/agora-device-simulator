"""Replacement probes for `cms_client.service` module-level helpers."""

from __future__ import annotations

from pathlib import Path

from sim.shims.profile import current_profile


def get_storage_mb(path: Path) -> tuple[int, int]:
    p = current_profile()
    return p.storage_total_mb, p.storage_used_mb


def get_cpu_temp() -> float | None:
    return current_profile().cpu_temp_c


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
