"""Fake player — replaces the real `player` systemd service.

Watches `DesiredState` written by `cms_client` and mirrors it into `CurrentState`
so that cms_client's `_player_watch_loop` can detect transitions and send
PLAYBACK_STARTED / PLAYBACK_ENDED to the CMS.

Also simulates loop-count playback: when `loop_count=N`, the fake player sleeps
for `fake_asset_duration_sec * N` seconds and then flips `CurrentState.mode` to
"splash" so cms_client observes the end-of-stream and triggers re-evaluation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from shared.models import CurrentState, DesiredState, PlaybackMode
from shared.state import read_state, write_state

logger = logging.getLogger("agora_sim.fake_player")

POLL_INTERVAL = 1.0


class FakePlayer:
    """Minimal DesiredState -> CurrentState mirror with fake playback timing."""

    def __init__(
        self,
        desired_path: Path,
        current_path: Path,
        *,
        fake_asset_duration_sec: float = 10.0,
    ) -> None:
        self.desired_path = desired_path
        self.current_path = current_path
        self.fake_asset_duration_sec = fake_asset_duration_sec
        self._running = False
        self._last_desired_ts: datetime | None = None
        self._play_started_at: datetime | None = None
        self._loops_done = 0
        self._current_asset: str | None = None
        self._current_loop_count: int | None = None

    async def run(self) -> None:
        self._running = True
        self._write_current(CurrentState(mode=PlaybackMode.SPLASH, display_connected=True))

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
                display_connected=True,
            ))
        elif desired.mode == PlaybackMode.SPLASH:
            self._reset_playback()
            self._write_current(CurrentState(mode=PlaybackMode.SPLASH, display_connected=True))
        else:  # STOP
            self._reset_playback()
            self._write_current(CurrentState(mode=PlaybackMode.STOP, display_connected=True))

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
                    display_connected=True,
                ))

    def _reset_playback(self) -> None:
        self._play_started_at = None
        self._loops_done = 0
        self._current_asset = None
        self._current_loop_count = None

    def _write_current(self, state: CurrentState) -> None:
        try:
            write_state(self.current_path, state)
        except Exception:
            logger.exception("Failed to write current state")
