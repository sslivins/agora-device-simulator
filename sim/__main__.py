"""CLI entry point: `python -m sim` or `agora-sim`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import tempfile
from pathlib import Path

import click

DEFAULT_PERSIST_ROOT = Path(tempfile.gettempdir()) / "agora-sim"

# IMPORTANT: sys.path setup + apply_shims must happen before any cms_client
# import. We do it here before the launcher (which imports DeviceInstance
# which imports cms_client) is pulled in.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGORA_ROOT = _REPO_ROOT / "agora"
if str(_AGORA_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGORA_ROOT))

from sim.shims import apply_shims  # noqa: E402

apply_shims()

from sim.launcher import Launcher  # noqa: E402


@click.command()
@click.option("--count", type=int, default=1, show_default=True,
              help="Number of simulated devices to spawn.")
@click.option("--cms-url", required=True,
              help="CMS WebSocket URL, e.g. ws://localhost:8080/ws/device")
@click.option("--serial-prefix", default="sim", show_default=True,
              help="Prefix for generated device serials (e.g. 'sim' -> 'sim-00042').")
@click.option("--board", type=click.Choice(["zero_2w", "pi_4", "pi_5"]),
              default="pi_5", show_default=True,
              help="Simulated Pi board model.")
@click.option("--ramp-rate", type=float, default=10.0, show_default=True,
              help="Devices per second to start (avoids thundering-herd).")
@click.option("--persist-root", type=click.Path(file_okay=False, path_type=Path),
              default=DEFAULT_PERSIST_ROOT, show_default=True,
              help="Root dir for per-instance state (each device gets a subdir).")
@click.option("--asset-budget-mb", type=int, default=200, show_default=True,
              help="Per-device asset budget in MB (controls eviction).")
@click.option("--fake-duration", type=float, default=10.0, show_default=True,
              help="Fake asset playback duration in seconds (for loop-count sims).")
@click.option("--control-host", default="127.0.0.1", show_default=True,
              help="Bind address for the HTTP control plane.")
@click.option("--control-port", type=int, default=9090, show_default=True,
              help="Port for the HTTP fault-injection control plane (0 disables).")
@click.option("--keep-state/--cleanup-state", default=False,
              help="Keep per-device state dirs after shutdown (useful for debugging).")
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
def main(
    count: int,
    cms_url: str,
    serial_prefix: str,
    board: str,
    ramp_rate: float,
    persist_root: Path,
    asset_budget_mb: int,
    fake_duration: float,
    control_host: str,
    control_port: int,
    keep_state: bool,
    log_level: str,
) -> None:
    """Launch N simulated Agora devices against a CMS."""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    launcher = Launcher(
        count=count,
        cms_url=cms_url,
        serial_prefix=serial_prefix,
        board=board,
        ramp_rate_per_sec=ramp_rate,
        persist_root=persist_root,
        asset_budget_mb=asset_budget_mb,
        fake_asset_duration_sec=fake_duration,
        cleanup_on_stop=not keep_state,
        control_host=control_host,
        control_port=control_port,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(launcher.run())

    def _sigint(*_: object) -> None:
        logging.getLogger("agora_sim").info("Received shutdown signal")
        for t in asyncio.all_tasks(loop):
            t.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _sigint)
        loop.add_signal_handler(signal.SIGTERM, _sigint)
    except (NotImplementedError, AttributeError):
        # Windows doesn't support add_signal_handler on ProactorEventLoop;
        # KeyboardInterrupt from Ctrl-C still propagates via run_until_complete.
        pass

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.run_until_complete(launcher.stop())
        loop.close()


if __name__ == "__main__":
    main()
