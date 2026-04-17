"""Monkey-patches that replace hardware-specific agora modules with simulator fakes.

The shims are applied by `apply_shims()` which MUST be called before importing
`cms_client.service` or `api.config`. After that, per-instance behavior is
configured through `DeviceProfile` values stored in a ContextVar so that many
simulated devices can coexist in the same process.
"""

from sim.shims.profile import DeviceProfile, current_profile, set_profile
from sim.shims.installer import apply_shims

__all__ = ["DeviceProfile", "apply_shims", "current_profile", "set_profile"]
