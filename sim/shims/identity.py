"""Replacement for `shared.identity`."""

from sim.shims.profile import current_profile


def get_device_serial() -> str:
    return current_profile().serial


def get_device_serial_suffix(length: int = 4) -> str:
    serial = get_device_serial()
    if serial == "unknown":
        return "0000"
    return serial[-length:].upper()
