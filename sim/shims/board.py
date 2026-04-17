"""Replacement for `shared.board` — returns values from the active DeviceProfile."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from sim.shims.profile import current_profile


class Board(str, enum.Enum):
    ZERO_2W = "zero_2w"
    PI_4 = "pi_4"
    PI_5 = "pi_5"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HdmiPort:
    name: str
    i2c_bus: str


_CONFIG: dict[str, dict] = {
    "zero_2w": {
        "hdmi_ports": [HdmiPort("HDMI-0", "/dev/null")],
        "codecs": ["h264"],
        "has_wifi": True,
        "has_ethernet": False,
        "max_fps": 30,
        "player_backend": "gstreamer",
        "alsa_card": "vc4hdmi",
    },
    "pi_4": {
        "hdmi_ports": [
            HdmiPort("HDMI-0", "/dev/null"),
            HdmiPort("HDMI-1", "/dev/null"),
        ],
        "codecs": ["hevc", "h264"],
        "has_wifi": True,
        "has_ethernet": True,
        "max_fps": 30,
        "player_backend": "mpv",
        "alsa_card": "vc4hdmi",
    },
    "pi_5": {
        "hdmi_ports": [
            HdmiPort("HDMI-0", "/dev/null"),
            HdmiPort("HDMI-1", "/dev/null"),
        ],
        "codecs": ["hevc"],
        "has_wifi": False,
        "has_ethernet": True,
        "max_fps": 60,
        "player_backend": "mpv",
        "alsa_card": "vc4hdmi0",
    },
}


def _cfg() -> dict:
    return _CONFIG.get(current_profile().board, _CONFIG["pi_5"])


def get_board() -> Board:
    return Board(current_profile().board)


def get_i2c_bus() -> str:
    return _cfg()["hdmi_ports"][0].i2c_bus


def get_i2c_buses() -> list[HdmiPort]:
    return list(_cfg()["hdmi_ports"])


def hdmi_port_count() -> int:
    return len(_cfg()["hdmi_ports"])


def supported_codecs() -> list[str]:
    fault = current_profile().fault
    if fault.codecs is not None:
        return list(fault.codecs)
    return list(_cfg()["codecs"])


def has_wifi() -> bool:
    return _cfg()["has_wifi"]


def has_ethernet() -> bool:
    return _cfg()["has_ethernet"]


def max_fps() -> int:
    return _cfg()["max_fps"]


def player_backend() -> str:
    return _cfg()["player_backend"]


def alsa_card() -> str:
    return _cfg()["alsa_card"]


def get_cpu_temp() -> float | None:
    prof = current_profile()
    if prof.fault.cpu_temp is not None:
        return prof.fault.cpu_temp
    return prof.cpu_temp_c
