"""Spawn and manage many DeviceInstances concurrently."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from sim.instance import DeviceInstance
from sim.shims import DeviceProfile

DEFAULT_PERSIST_ROOT = Path(tempfile.gettempdir()) / "agora-sim"

logger = logging.getLogger("agora_sim.launcher")


class Launcher:
    def __init__(
        self,
        *,
        count: int,
        cms_url: str,
        serial_prefix: str = "sim",
        board: str = "pi_5",
        ramp_rate_per_sec: float = 10.0,
        persist_root: Path = DEFAULT_PERSIST_ROOT,
        asset_budget_mb: int = 200,
        fake_asset_duration_sec: float = 10.0,
        cleanup_on_stop: bool = True,
        control_host: str = "127.0.0.1",
        control_port: int = 9090,
    ) -> None:
        self.count = count
        self.cms_url = cms_url
        self.serial_prefix = serial_prefix
        self.board = board
        self.ramp_rate_per_sec = ramp_rate_per_sec
        self.persist_root = persist_root
        self.asset_budget_mb = asset_budget_mb
        self.fake_asset_duration_sec = fake_asset_duration_sec
        self.cleanup_on_stop = cleanup_on_stop
        self.control_host = control_host
        self.control_port = control_port
        self._instances: list[DeviceInstance] = []
        self._tasks: list[asyncio.Task] = []
        self._control_runner = None

    def _build_profile(self, i: int) -> DeviceProfile:
        serial = f"{self.serial_prefix}-{i:05d}"
        return DeviceProfile(
            serial=serial,
            board=self.board,
            local_ip=f"10.0.{(i >> 8) & 0xff}.{i & 0xff}",
            persist_root=self.persist_root,
        )

    async def run(self) -> None:
        delay = 1.0 / self.ramp_rate_per_sec if self.ramp_rate_per_sec > 0 else 0

        if self.control_port > 0:
            from sim.control import start_control_plane
            self._control_runner = await start_control_plane(
                self.control_host, self.control_port)

        for i in range(self.count):
            profile = self._build_profile(i)
            instance = DeviceInstance(
                profile=profile,
                cms_url=self.cms_url,
                asset_budget_mb=self.asset_budget_mb,
                fake_asset_duration_sec=self.fake_asset_duration_sec,
                cleanup_on_stop=self.cleanup_on_stop,
            )
            self._instances.append(instance)
            task = asyncio.create_task(
                self._run_instance_in_context(instance),
                name=f"inst-{profile.serial}",
            )
            self._tasks.append(task)
            if delay and i < self.count - 1:
                await asyncio.sleep(delay)

        logger.info("Launched %d simulated devices", self.count)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            await self.stop()

    async def _run_instance_in_context(self, instance: DeviceInstance) -> None:
        """Run a DeviceInstance in its own task context.

        The instance sets a ContextVar (current_profile) inside `run()`; each
        task gets its own copy of context so the profiles never collide.
        """
        try:
            await instance.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] crashed", instance.profile.serial)

    async def stop(self) -> None:
        logger.info("Stopping launcher")
        if self._control_runner is not None:
            try:
                await self._control_runner.cleanup()
            except Exception:
                logger.debug("Error cleaning up control plane", exc_info=True)
            self._control_runner = None
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for inst in self._instances:
            try:
                await inst.stop()
            except Exception:
                pass
