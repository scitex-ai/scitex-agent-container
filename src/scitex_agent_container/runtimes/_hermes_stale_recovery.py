"""Bounded in-place recovery for Hermes' cross-turn stale-provider latch.

Hermes 0.21.1 exposes no external RPC for its in-memory stale streak.  Its
supported ``/model`` command does reset that streak after rebuilding the live
client, including a same-provider/session switch.  This adapter therefore uses
the narrow observable boundary available to an owning TUI: the exact upstream
terminal error, followed by that supported command once the configured
provider's health endpoint proves immediate admission capacity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..config import AgentConfig, load_config
from ._runtime_control import READY, RECOVERING, STALE_LATCHED, write_control_state

log = logging.getLogger(__name__)

PID_FILENAME = "hermes-recovery.pid"
LOG_FILENAME = "hermes-recovery.log"
MODULE_PATH = "scitex_agent_container.runtimes._hermes_stale_recovery"
POLL_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 3.0
_STALE_RE = re.compile(
    r"Provider has been unresponsive(?:[^\n]*?)for\s+(\d+)\s+consecutive stale attempts",
    re.IGNORECASE,
)
_STOP_EVENT = threading.Event()


def _state_dir(config: AgentConfig) -> Path:
    from .tui_session import state_dir_for_config

    return state_dir_for_config(config)


def _pid_path(config: AgentConfig) -> Path:
    return _state_dir(config) / PID_FILENAME


def _owns_monitor_process(pid: int, config_path: str) -> bool:
    """Prove a recorded PID is this module for this exact agent spec."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    decoded = [part.decode(errors="replace") for part in argv if part]
    return MODULE_PATH in decoded and config_path in decoded


def recovery_command(config: AgentConfig) -> str:
    """The documented Hermes same-session provider rebind."""
    provider = f"sac-{config.engine_key or config.model}"
    return f"/model {config.model} --provider {provider} --session"


def health_url(config: AgentConfig) -> str:
    """Map the configured inference API root to its service health route."""
    raw = str(config.claude.provider.base_url or "").rstrip("/")
    parsed = urlsplit(raw)
    path = parsed.path.removesuffix("/v1").rstrip("/") + "/health"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def provider_has_capacity(
    config: AgentConfig,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout_s: float = PROBE_TIMEOUT_SECONDS,
) -> bool:
    """One bounded, non-generating health probe with conservative admission."""
    request = urllib.request.Request(health_url(config), method="GET")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            if int(getattr(response, "status", 200)) != 200:
                return False
            payload = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    members = payload.get("members")
    if not isinstance(members, list):
        # Process health alone does not prove admission.  Remain latched until
        # the configured provider can state capacity explicitly.
        return False
    return any(
        isinstance(member, dict)
        and member.get("active") is True
        and isinstance(member.get("capacity"), int)
        and isinstance(member.get("in_flight"), int)
        and isinstance(member.get("queued", 0), int)
        and member["in_flight"] + member.get("queued", 0) < member["capacity"]
        for member in members
    )


def stale_latch(pane: str) -> tuple[int, str] | None:
    """Return streak + occurrence fingerprint for the exact Hermes error."""
    matches = list(_STALE_RE.finditer(pane))
    if not matches:
        return None
    match = matches[-1]
    # Hash through the end of the occurrence, not text after it.  Hermes adds
    # the successful /model output below the old error; that must not turn one
    # occurrence into a second recovery.  A genuinely later error necessarily
    # has the intervening transcript in its prefix and therefore a new hash.
    occurrence = pane[: match.end()]
    return int(match.group(1)), hashlib.sha256(occurrence.encode()).hexdigest()


def recovery_tick(
    config: AgentConfig,
    *,
    capture: Callable[[], str],
    pause: Callable[[], bool],
    recover: Callable[[], bool],
    probe: Callable[[AgentConfig], bool] = provider_has_capacity,
    now: Callable[[], float] = time.time,
    previous_fingerprint: str = "",
    state_dir: Path | None = None,
) -> str:
    """Observe once and perform at most one probe and one recovery action."""
    state_dir = state_dir or _state_dir(config)
    observed = stale_latch(capture())
    if observed is None:
        if previous_fingerprint:
            write_control_state(
                state_dir,
                {
                    "turn_admission": READY,
                    "detail": "same session admitted a turn after provider recovery",
                    "observed_at": now(),
                },
            )
        return previous_fingerprint
    streak, fingerprint = observed
    recovered_token = f"recovered:{fingerprint}"
    latched_token = f"latched:{fingerprint}"
    if previous_fingerprint == recovered_token:
        return previous_fingerprint
    stamp = now()
    write_control_state(
        state_dir,
        {
            "turn_admission": STALE_LATCHED,
            "detail": f"provider stale circuit breaker latched after {streak} attempts",
            "observed_at": stamp,
        },
    )
    if previous_fingerprint != latched_token:
        # Pause first and probe on the next bounded tick.  Hermes documents
        # pause/resume as session-scoped controls; separating pause from the
        # health probe prevents a just-due heartbeat racing the rebind.
        return latched_token if pause() else ""
    if not probe(config):
        return latched_token
    if recover():
        write_control_state(
            state_dir,
            {
                "turn_admission": RECOVERING,
                "detail": "provider healthy; same-session provider rebind submitted",
                "observed_at": stamp,
            },
        )
        return recovered_token
    return latched_token


def _stable_initial_delay(name: str) -> float:
    digest = hashlib.sha256(name.encode()).digest()
    return float(int.from_bytes(digest[:8], "big") % int(POLL_SECONDS))


def run_monitor(config: AgentConfig) -> None:
    """Run one rate-bounded observer for the life of the owning tmux session."""
    from .._runners._tmux.tmux import TmuxManager
    from .hermes_tui import HermesTuiSessionRuntime

    runtime = HermesTuiSessionRuntime()
    mux = TmuxManager()
    session = runtime.session_name(config)
    if not session:
        return
    if _STOP_EVENT.wait(_stable_initial_delay(config.name)):
        return
    recovered_fingerprint = ""
    while mux.exists(session):
        try:
            recovered_fingerprint = recovery_tick(
                config,
                capture=lambda: mux.capture_content(session),
                pause=lambda: bool(runtime.suspend_autonomous_turns(config)),
                recover=lambda: bool(runtime.recover_turn_admission(config)),
                previous_fingerprint=recovered_fingerprint,
            )
        except Exception:  # stx-allow: fallback (reason: one observation/control failure must not kill the bounded recovery monitor)
            log.exception("Hermes recovery tick failed for %s", config.name)
        if _STOP_EVENT.wait(POLL_SECONDS):
            return


def start_recovery_monitor(
    config: AgentConfig, *, spawn: Callable[..., Any] = subprocess.Popen
) -> int | None:
    """Start one detached monitor for an autonomous Hermes TUI."""
    autonomous = getattr(config, "autonomous", None)
    if not getattr(autonomous, "enabled", False):
        return None
    stop_recovery_monitor(config)
    config_path = str(getattr(config, "config_path", "") or "")
    if not config_path:
        log.warning("Hermes recovery monitor: no config_path for %r", config.name)
        return None
    state_dir = _state_dir(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    write_control_state(
        state_dir,
        {
            "turn_admission": READY,
            "detail": "Hermes recovery observer started",
            "observed_at": time.time(),
        },
    )
    stream = open(state_dir / LOG_FILENAME, "ab")
    try:
        process = spawn(
            [sys.executable, "-m", MODULE_PATH, "--config-path", config_path],
            stdout=stream,
            stderr=stream,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        stream.close()
        log.warning("Hermes recovery monitor failed to start for %r: %s", config.name, exc)
        return None
    stream.close()
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        _pid_path(config).write_text(f"{pid}\n", encoding="utf-8")
        return pid
    return None


def stop_recovery_monitor(config: AgentConfig) -> bool:
    """Stop only the PID recorded for this agent's recovery observer."""
    state_dir = _state_dir(config)
    path = _pid_path(config)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        if state_dir.exists():
            write_control_state(
                state_dir,
                {
                    "turn_admission": READY,
                    "detail": "Hermes recovery observer is not running",
                    "observed_at": time.time(),
                },
            )
        return False
    config_path = str(getattr(config, "config_path", "") or "")
    if not _owns_monitor_process(pid, config_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        write_control_state(
            state_dir,
            {
                "turn_admission": READY,
                "detail": "Hermes recovery observer is not running",
                "observed_at": time.time(),
            },
        )
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        stopped = True
    except ProcessLookupError:
        stopped = False
    except OSError as exc:
        log.warning("Hermes recovery monitor: SIGTERM pid %d failed: %s", pid, exc)
        stopped = False
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    write_control_state(
        state_dir,
        {
            "turn_admission": READY,
            "detail": "Hermes recovery observer stopped",
            "observed_at": time.time(),
        },
    )
    return stopped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config_path)
    signal.signal(signal.SIGTERM, lambda *_args: _STOP_EVENT.set())
    signal.signal(signal.SIGINT, lambda *_args: _STOP_EVENT.set())
    try:
        run_monitor(config)
    finally:
        path = _pid_path(config)
        try:
            recorded = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            recorded = -1
        if recorded == os.getpid():
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())


__all__ = [
    "health_url",
    "provider_has_capacity",
    "recovery_command",
    "recovery_tick",
    "stale_latch",
    "start_recovery_monitor",
    "stop_recovery_monitor",
]
