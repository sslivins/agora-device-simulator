"""Per-instance device profile held in a ContextVar.

Each simulated device runs inside an asyncio task that sets `CURRENT_PROFILE`
before calling into cms_client code. The board/identity/probe shims read
`current_profile()` to return instance-specific fake values.
"""

from __future__ import annotations

import contextvars
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Optional

_DEFAULT_PERSIST_ROOT = Path(tempfile.gettempdir()) / "agora-sim"


@dataclass
class FaultState:
    """Runtime fault injection overrides.

    Any value set to None means "use the DeviceProfile default".
    Faults are mutated by the control-plane HTTP API and read by the shims.
    """

    cpu_temp: Optional[float] = None
    storage_mb_free: Optional[int] = None
    codecs: Optional[list[str]] = None
    offline_until: Optional[float] = None  # asyncio loop time
    asset_fetch_fail_count: int = 0
    heartbeat_stalled: bool = False

    def to_dict(self) -> dict:
        return {
            "cpu_temp": self.cpu_temp,
            "storage_mb_free": self.storage_mb_free,
            "codecs": list(self.codecs) if self.codecs is not None else None,
            "offline_until": self.offline_until,
            "asset_fetch_fail_count": self.asset_fetch_fail_count,
            "heartbeat_stalled": self.heartbeat_stalled,
        }

    def clear(self) -> None:
        self.cpu_temp = None
        self.storage_mb_free = None
        self.codecs = None
        self.offline_until = None
        self.asset_fetch_fail_count = 0
        self.heartbeat_stalled = False


RECORDER_MAX_COMMANDS = 100


@dataclass
class CommandRecorder:
    """In-memory record of WS commands received by a simulated device.

    Populated by the recording shim (see sim.shims.installer) each time the
    CMS dispatches a message to the device. Tests query this via the control
    plane to assert round-trips (e.g. "clicking Reboot in the UI caused the
    device to receive a reboot command"), without touching production code.
    """

    commands: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RECORDER_MAX_COMMANDS)
    )
    counters: Dict[str, int] = field(default_factory=dict)
    last_config: Dict[str, Any] = field(default_factory=dict)

    def record(self, msg_type: str, payload: Dict[str, Any]) -> None:
        self.commands.append({
            "type": msg_type,
            "payload": payload,
            "ts": time.time(),
        })
        self.counters[msg_type] = self.counters.get(msg_type, 0) + 1
        if msg_type == "config":
            for k, v in payload.items():
                if k == "type":
                    continue
                self.last_config[k] = v

    def reset(self) -> None:
        self.commands.clear()
        self.counters.clear()
        self.last_config.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": len(self.commands),
            "counters": dict(self.counters),
            "last_config": dict(self.last_config),
            "commands": list(self.commands),
        }


@dataclass
class DeviceProfile:
    """Configuration for a single simulated device."""

    serial: str
    board: str = "pi_5"
    model_string: str = "Raspberry Pi 5 Model B Rev 1.0"
    local_ip: str = "10.0.0.1"
    cpu_temp_c: float = 45.0
    storage_total_mb: int = 16 * 1024
    storage_used_mb: int = 2 * 1024
    ssh_enabled: bool = False
    persist_root: Path = field(default_factory=lambda: _DEFAULT_PERSIST_ROOT)
    fault: FaultState = field(default_factory=FaultState)
    recorder: CommandRecorder = field(default_factory=CommandRecorder)


_CURRENT_PROFILE: contextvars.ContextVar[DeviceProfile | None] = contextvars.ContextVar(
    "agora_sim_current_profile", default=None,
)


def set_profile(profile: DeviceProfile) -> contextvars.Token:
    return _CURRENT_PROFILE.set(profile)


def current_profile() -> DeviceProfile:
    prof = _CURRENT_PROFILE.get()
    if prof is None:
        raise RuntimeError(
            "No simulator DeviceProfile bound to this context. "
            "Call sim.shims.set_profile(...) before entering cms_client code."
        )
    return prof
