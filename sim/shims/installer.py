"""Install the simulator shims into the agora import namespace.

Must be called exactly once, BEFORE importing `cms_client.service` or any
module that pulls in `shared.board` / `shared.identity`. Typically called
from `sim/__main__.py` after setting up sys.path to include the agora
submodule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

_APPLIED = False
logger = logging.getLogger("agora_sim.shims")


def apply_shims() -> None:
    global _APPLIED
    if _APPLIED:
        return

    from sim.shims import board as shim_board
    from sim.shims import identity as shim_identity

    sys.modules["shared.board"] = shim_board
    sys.modules["shared.identity"] = shim_identity

    from cms_client import service as cms_service
    from sim.shims import probes
    from sim.shims.profile import current_profile

    cms_service._get_storage_mb = probes.get_storage_mb
    cms_service._get_cpu_temp = probes.get_cpu_temp
    cms_service._is_ssh_enabled = probes.is_ssh_enabled
    cms_service._get_local_ip = probes.get_local_ip
    cms_service._get_device_id = probes.get_device_id
    cms_service._get_device_type = probes.get_device_type
    cms_service.CMSClient._apply_timezone = lambda self, tz_name: None

    _install_fault_hooks(cms_service, current_profile)
    _install_recording_hooks(cms_service, current_profile)
    _install_logs_synth_hook(cms_service, current_profile)
    _install_logs_branch_counters(cms_service, current_profile)

    _APPLIED = True


_RECORDED_HANDLERS = (
    ("_handle_auth_assigned", "auth_assigned", True),
    ("_handle_sync", "sync", True),
    ("_handle_play", "play", True),
    ("_handle_stop", "stop", False),
    ("_handle_fetch_asset", "fetch_asset", True),
    ("_handle_delete_asset", "delete_asset", True),
    ("_handle_config", "config", True),
    ("_handle_reboot", "reboot", False),
    ("_handle_upgrade", "upgrade", False),
    ("_handle_factory_reset", "factory_reset", False),
    ("_handle_wipe_assets", "wipe_assets", True),
    ("_handle_request_logs", "request_logs", True),
)


def _install_recording_hooks(cms_service, current_profile) -> None:
    """Wrap every CMS->device handler to log inbound commands for test assertions.

    Recording is per-device (via the current_profile ContextVar). If the shim
    is invoked outside a simulator context (no bound profile), we no-op and
    delegate straight through to the original handler.
    """
    CMSClient = cms_service.CMSClient

    def _record(msg_type: str, payload: dict) -> None:
        try:
            recorder = current_profile().recorder
        except RuntimeError:
            return
        recorder.record(msg_type, payload)

    for attr, msg_type, takes_msg in _RECORDED_HANDLERS:
        orig = getattr(CMSClient, attr, None)
        if orig is None:
            continue
        if takes_msg:
            def _make(orig_fn, t):
                async def wrapper(self, msg, *args, **kwargs):
                    _record(t, msg if isinstance(msg, dict) else {"_raw": str(msg)})
                    return await orig_fn(self, msg, *args, **kwargs)
                return wrapper
            setattr(CMSClient, attr, _make(orig, msg_type))
        else:
            def _make_no_msg(orig_fn, t):
                async def wrapper(self, *args, **kwargs):
                    _record(t, {})
                    return await orig_fn(self, *args, **kwargs)
                return wrapper
            setattr(CMSClient, attr, _make_no_msg(orig, msg_type))


def _install_fault_hooks(cms_service, current_profile) -> None:
    """Wrap CMSClient methods so runtime faults can steer behaviour."""
    CMSClient = cms_service.CMSClient

    # ---- offline: block (re)connect until fault deadline passes ----
    _orig_connect_and_run = CMSClient._connect_and_run

    async def _connect_and_run_faulted(self):
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.offline_until is not None:
            loop = asyncio.get_event_loop()
            remaining = fault.offline_until - loop.time()
            if remaining > 0:
                logger.info("[%s] offline fault: sleeping %.1fs before reconnect",
                            self.device_id, remaining)
                await asyncio.sleep(remaining)
            fault.offline_until = None
        return await _orig_connect_and_run(self)

    CMSClient._connect_and_run = _connect_and_run_faulted

    # ---- heartbeat stall: skip sending status messages while active ----
    _orig_send_status = CMSClient._send_status

    async def _send_status_faulted(self):
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.heartbeat_stalled:
            return  # drop the heartbeat silently
        return await _orig_send_status(self)

    CMSClient._send_status = _send_status_faulted

    # ---- asset fetch failure: synthesize a fetch_failed response ----
    _orig_handle_fetch_asset = CMSClient._handle_fetch_asset

    async def _handle_fetch_asset_faulted(self, msg: dict, ws) -> None:
        try:
            fault = current_profile().fault
        except RuntimeError:
            fault = None
        if fault and fault.asset_fetch_fail_count > 0:
            fault.asset_fetch_fail_count -= 1
            asset_name = msg.get("asset_name", "")
            logger.info("[%s] injected asset_fetch_fail for %s",
                        self.device_id, asset_name)
            fail = {
                "type": "fetch_failed",
                "protocol_version": cms_service.PROTOCOL_VERSION,
                "device_id": self.device_id,
                "asset": asset_name,
                "reason": "simulated_fault",
            }
            try:
                await ws.send(json.dumps(fail))
            except Exception:
                logger.debug("Could not send synthesized fetch_failed", exc_info=True)
            return
        return await _orig_handle_fetch_asset(self, msg, ws)

    CMSClient._handle_fetch_asset = _handle_fetch_asset_faulted


_FILLER_LINE = "agora-sim-log-line\n"
_MAX_SYNTH_BYTES = 25 * 1024 * 1024  # 25 MiB hard ceiling per service


def _synth_bytes(service: str, value) -> str:
    """Render a logs_synth entry into log text.

    int  → exactly N bytes of repeating ASCII filler (so tests can size
           the payload precisely against firmware thresholds).
    str  → literal text content.
    Anything else raises TypeError.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(
            f"logs_synth[{service!r}] must be int (bytes) or str, got {type(value).__name__}"
        )
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"logs_synth[{service!r}] must be >= 0, got {value}")
        if value > _MAX_SYNTH_BYTES:
            raise ValueError(
                f"logs_synth[{service!r}] {value} exceeds {_MAX_SYNTH_BYTES}-byte cap"
            )
        if value == 0:
            return ""
        # Build content prefixed with the service name so each entry is
        # distinguishable in the assembled tarball, then pad/truncate to
        # exactly N bytes of UTF-8.
        prefix = f"[{service}] "
        body = (prefix + _FILLER_LINE) * (value // (len(prefix) + len(_FILLER_LINE)) + 1)
        return body[:value]
    return value


def _install_logs_synth_hook(cms_service, current_profile) -> None:
    """Intercept ``journalctl`` shell-outs in ``_handle_request_logs``.

    When the active device profile has ``logs_synth`` set, we substitute
    the configured per-service content for what would otherwise be a
    ``FileNotFoundError`` (the sim container has no ``journalctl``).
    All other ``subprocess.run`` calls — and journalctl calls when the
    profile has no synth — fall through to the real ``subprocess.run``.

    We patch ``cms_service.subprocess.run`` (only ``.run``, not the
    whole module) so other names like ``CompletedProcess`` / ``TimeoutExpired``
    keep their real bindings.
    """
    real_run = cms_service.subprocess.run
    CompletedProcess = cms_service.subprocess.CompletedProcess

    def _faked_run(cmd, *args, **kwargs):
        # Only intercept journalctl invocations; everything else passes through.
        try:
            is_journalctl = (
                isinstance(cmd, (list, tuple))
                and len(cmd) >= 1
                and cmd[0] == "journalctl"
            )
        except Exception:
            is_journalctl = False
        if not is_journalctl:
            return real_run(cmd, *args, **kwargs)

        try:
            profile = current_profile()
        except RuntimeError:
            profile = None
        synth = getattr(profile, "logs_synth", None) if profile is not None else None
        if not synth:
            return real_run(cmd, *args, **kwargs)

        # Extract the service name from ``journalctl -u <service> ...``.
        service = ""
        for i, tok in enumerate(cmd):
            if tok == "-u" and i + 1 < len(cmd):
                service = cmd[i + 1]
                break
        if service not in synth:
            # Service not synthesized for this device — emit empty log
            # rather than fall through to a real journalctl that doesn't
            # exist (would surface as FileNotFoundError and abort the
            # whole batch in the firmware).
            stdout = ""
        else:
            try:
                stdout = _synth_bytes(service, synth[service])
            except (TypeError, ValueError) as e:
                logger.warning("logs_synth coercion failed for %s: %s", service, e)
                stdout = f"[sim logs_synth error: {e}]"
        return CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    cms_service.subprocess.run = _faked_run


def _install_logs_branch_counters(cms_service, current_profile) -> None:
    """Increment per-branch counters when a log request actually completes.

    These let nightly tests *prove* which firmware path ran: today both
    success branches end with a CMS row reaching ``ready``, so the
    download alone can't distinguish small-JSON-WS from large-HTTP-upload.

    Counters land on the active profile's recorder under:
      - ``logs_ws_json``  — every successful small ``logs_response`` send
      - ``logs_upload``   — every successful HTTP tarball upload
    """
    CMSClient = cms_service.CMSClient
    _orig_upload = CMSClient._upload_logs_bundle

    async def _upload_counted(self, request_id, tar_gz):
        result = await _orig_upload(self, request_id, tar_gz)
        try:
            recorder = current_profile().recorder
        except RuntimeError:
            return result
        recorder.counters["logs_upload"] = recorder.counters.get("logs_upload", 0) + 1
        return result

    CMSClient._upload_logs_bundle = _upload_counted

    # For the small-JSON branch there is no dedicated method to wrap;
    # we wrap _handle_request_logs and inspect the outcome by sniffing
    # the payload size against the firmware threshold *before* the call.
    # That way we count the JSON branch only when the firmware would
    # actually take it (and the upload branch is independently counted
    # via _upload_logs_bundle, so we don't double-count).
    _orig_handle = CMSClient._handle_request_logs

    async def _handle_counted(self, msg, ws):
        result = await _orig_handle(self, msg, ws)
        try:
            profile = current_profile()
        except RuntimeError:
            return result
        synth = getattr(profile, "logs_synth", None)
        if not synth:
            return result
        # Replicate the firmware sizing check: services list either from
        # the message or the firmware default. Synthesized stdout is
        # what subprocess.run returned above.
        services = msg.get("services") or [
            "agora-player", "agora-api", "agora-cms-client", "agora-provision",
        ]
        try:
            logs = {s: _synth_bytes(s, synth[s]) for s in services if s in synth}
        except Exception:
            return result
        for s in services:
            logs.setdefault(s, "")
        response = {
            "type": "logs_response",
            "protocol_version": cms_service.PROTOCOL_VERSION,
            "request_id": msg.get("request_id", ""),
            "device_id": self.device_id,
            "logs": logs,
            "error": None,
        }
        json_size = len(json.dumps(response).encode("utf-8"))
        if json_size <= cms_service.LOGS_JSON_MAX_BYTES:
            profile.recorder.counters["logs_ws_json"] = (
                profile.recorder.counters.get("logs_ws_json", 0) + 1
            )
        return result

    CMSClient._handle_request_logs = _handle_counted


