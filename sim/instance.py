"""A single simulated device — isolated persist dir, CMSClient + FakePlayer."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from sim.shims import DeviceProfile, set_profile

logger = logging.getLogger("agora_sim.instance")

# Module-level registry: serial -> DeviceInstance. Populated on run(), removed
# on stop(). Used by the control-plane HTTP API to look up devices.
INSTANCES: dict[str, "DeviceInstance"] = {}


class DeviceInstance:
    """One simulated Agora device."""

    def __init__(
        self,
        profile: DeviceProfile,
        cms_url: str,
        *,
        asset_budget_mb: int = 200,
        fake_asset_duration_sec: float = 10.0,
        cleanup_on_stop: bool = True,
    ) -> None:
        self.profile = profile
        self.cms_url = cms_url
        self.asset_budget_mb = asset_budget_mb
        self.fake_asset_duration_sec = fake_asset_duration_sec
        self.cleanup_on_stop = cleanup_on_stop
        self.agora_base = profile.persist_root / profile.serial
        self._client = None
        self._fake_player = None
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        """Bind the profile to the current task, then run CMSClient + FakePlayer."""
        set_profile(self.profile)

        from api.config import Settings
        from cms_client.service import CMSClient
        from sim.fake_player import FakePlayer

        self.agora_base.mkdir(parents=True, exist_ok=True)

        settings = Settings(
            agora_base=self.agora_base,
            cms_url=self.cms_url,
            device_name=f"sim-{self.profile.serial}",
            asset_budget_mb=self.asset_budget_mb,
        )
        settings.ensure_dirs()

        self._fake_player = FakePlayer(
            desired_path=settings.desired_state_path,
            current_path=settings.current_state_path,
            fake_asset_duration_sec=self.fake_asset_duration_sec,
        )
        self._client = CMSClient(settings)

        INSTANCES[self.profile.serial] = self
        logger.info("[%s] starting (agora_base=%s, cms=%s)",
                    self.profile.serial, self.agora_base, self.cms_url)

        player_task = asyncio.create_task(
            self._fake_player.run(), name=f"player-{self.profile.serial}")
        client_task = asyncio.create_task(
            self._client.run(), name=f"client-{self.profile.serial}")
        self._tasks = [player_task, client_task]

        try:
            done, pending = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception() is not None:
                    logger.error("[%s] task failed: %s",
                                 self.profile.serial, t.exception())
        finally:
            await self.stop()

    async def set_offline(self, duration_sec: float) -> None:
        """Force this device offline: close WS and block reconnect for N sec."""
        loop = asyncio.get_event_loop()
        self.profile.fault.offline_until = loop.time() + duration_sec
        ws = getattr(self._client, "_ws", None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("[%s] error closing ws for offline fault",
                             self.profile.serial, exc_info=True)

    async def stop(self) -> None:
        INSTANCES.pop(self.profile.serial, None)
        if self._fake_player:
            self._fake_player.stop()
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("[%s] error stopping client", self.profile.serial, exc_info=True)
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.cleanup_on_stop and self.agora_base.exists():
            shutil.rmtree(self.agora_base, ignore_errors=True)
        logger.info("[%s] stopped", self.profile.serial)
