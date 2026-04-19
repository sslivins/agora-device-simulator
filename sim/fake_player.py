"""Fake player — replaces the real `player` systemd service.

Watches `DesiredState` written by `cms_client` and mirrors it into `CurrentState`
so that cms_client's `_player_watch_loop` can detect transitions and send
PLAYBACK_STARTED / PLAYBACK_ENDED to the CMS.

Also simulates loop-count playback: when `loop_count=N`, the fake player sleeps
for `fake_asset_duration_sec * N` seconds and then flips `CurrentState.mode` to
"splash" so cms_client observes the end-of-stream and triggers re-evaluation.

Display state honours the fault knobs:
  * `fault.display_connected` overrides the default (True) when not None.
  * `fault.display_ports` overrides the default port list when not None.
When either changes between ticks the player re-writes ``current.json`` so the
real cms_client's heartbeat loop forwards the new values to the CMS, exercising
the display-disconnect alert path end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from shared.models import CurrentState, DesiredState, PlaybackMode
from shared.state import atomic_write, read_state, write_state

from sim.shims.profile import DeviceProfile

logger = logging.getLogger("agora_sim.fake_player")

POLL_INTERVAL = 1.0

# Default display state when no fault is in effect.
_DEFAULT_DISPLAY_CONNECTED = True
_DEFAULT_DISPLAY_PORTS = ["HDMI-A-1"]


class FakePlayer:
    """Minimal DesiredState -> CurrentState mirror with fake playback timing."""

    def __init__(
        self,
        desired_path: Path,
        current_path: Path,
        *,
        fake_asset_duration_sec: float = 10.0,
        profile: Optional[DeviceProfile] = None,
    ) -> None:
        self.desired_path = desired_path
        self.current_path = current_path
        self.fake_asset_duration_sec = fake_asset_duration_sec
        self.profile = profile
        self._running = False
        self._last_desired_ts: datetime | None = None
        self._play_started_at: datetime | None = None
        self._loops_done = 0
        self._current_asset: str | None = None
        self._current_loop_count: int | None = None
        self._last_state: CurrentState | None = None
        self._last_display_connected: bool | None = None
        self._last_display_ports: list[str] | None = None

    # ── display-fault helpers ────────────────────────────────────────────

    def _effective_display_connected(self) -> bool:
        if self.profile is not None and self.profile.fault.display_connected is not None:
            return bool(self.profile.fault.display_connected)
        return _DEFAULT_DISPLAY_CONNECTED

    def _effective_display_ports(self) -> list[str]:
        if self.profile is not None and self.profile.fault.display_ports is not None:
            return list(self.profile.fault.display_ports)
        return list(_DEFAULT_DISPLAY_PORTS)

    async def run(self) -> None:
        self._running = True
        self._write_current(CurrentState(mode=PlaybackMode.SPLASH))

        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                desired = read_state(self.desired_path, DesiredState)
            except Exception:
                continue

            if self._last_desired_ts is None or desired.timestamp != self._last_desired_ts:
                self._last_desired_ts = desired.timestamp
                self._apply_desired(desired)

            self._tick_playback(desired)
            # Re-emit current.json if the display fault changed between ticks
            # so cms_client's next heartbeat reports the new values.
            self._reemit_if_display_changed()

    def stop(self) -> None:
        self._running = False

    def _apply_desired(self, desired: DesiredState) -> None:
        logger.debug("Applying desired: mode=%s asset=%s loop=%s loop_count=%s",
                     desired.mode, desired.asset, desired.loop, desired.loop_count)

        if desired.mode == PlaybackMode.PLAY and desired.asset:
            self._current_asset = desired.asset
            self._current_loop_count = desired.loop_count
            self._loops_done = 0
            self._play_started_at = datetime.now(timezone.utc)
            self._write_current(CurrentState(
                mode=PlaybackMode.PLAY,
                asset=desired.asset,
                loop=desired.loop,
                loop_count=desired.loop_count,
                loops_completed=0,
                started_at=self._play_started_at,
                pipeline_state="PLAYING",
            ))
        elif desired.mode == PlaybackMode.SPLASH:
            self._reset_playback()
            self._write_current(CurrentState(mode=PlaybackMode.SPLASH))
        else:  # STOP
            self._reset_playback()
            self._write_current(CurrentState(mode=PlaybackMode.STOP))

    def _tick_playback(self, desired: DesiredState) -> None:
        """Simulate asset duration and finite loop counts."""
        if self._play_started_at is None or self._current_loop_count is None:
            return
        elapsed = (datetime.now(timezone.utc) - self._play_started_at).total_seconds()
        loops_done = int(elapsed // self.fake_asset_duration_sec)
        if loops_done != self._loops_done:
            self._loops_done = loops_done
            if loops_done >= self._current_loop_count:
                logger.info("Fake playback finished %d loops → splash", loops_done)
                self._reset_playback()
                self._write_current(CurrentState(
                    mode=PlaybackMode.SPLASH,
                    loops_completed=loops_done,
                ))

    def _reemit_if_display_changed(self) -> None:
        """Re-write current.json if the display fault state changed.

        Without this, a display fault injected via the control plane would not
        be observable until the next desired-state transition. We want it to
        propagate via cms_client's regular heartbeat loop within ~1s of the
        fault being applied.
        """
        if self._last_state is None:
            return
        connected = self._effective_display_connected()
        ports = self._effective_display_ports()
        if (
            connected == self._last_display_connected
            and ports == self._last_display_ports
        ):
            return
        # Re-emit with the latest state but updated display fields.
        self._write_current(self._last_state)

    def _reset_playback(self) -> None:
        self._play_started_at = None
        self._loops_done = 0
        self._current_asset = None
        self._current_loop_count = None

    def _write_current(self, state: CurrentState) -> None:
        try:
            connected = self._effective_display_connected()
            ports = self._effective_display_ports()
            # Apply current display-fault values to whatever state we're writing.
            state.display_connected = connected
            # display_ports is not a field on CurrentState (cms_client reads it
            # straight from the JSON dict, so it just needs to land in the
            # file). Dump the model and inject the key by hand.
            payload = state.model_dump(mode="json")
            payload["display_ports"] = list(ports)
            atomic_write(
                self.current_path,
                json.dumps(payload, indent=2, default=str),
            )
            self._last_state = state
            self._last_display_connected = connected
            self._last_display_ports = list(ports)
        except Exception:
            logger.exception("Failed to write current state")
