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
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import scitex_logging as slogging

from ..config import AgentConfig, load_config
from ._hermes_tui_rpc import HermesSlashReceipt, HermesTurnOutcome, HermesTurnProgress
from ._runtime_control import READY, RECOVERING, STALE_LATCHED, write_control_state

log = slogging.getLogger(__name__)

PID_FILENAME = "hermes-recovery.pid"
LOG_FILENAME = "hermes-recovery.log"
MODULE_PATH = "scitex_agent_container.runtimes._hermes_stale_recovery"
POLL_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 3.0
_STALE_RE = re.compile(
    r"Provider has been unresponsive(?:(?!Provider has been unresponsive)[\s\S])*?"
    r"for\s+(\d+)\s+consecutive stale(?:\s|[│┃])+attempts",
    re.IGNORECASE,
)
_STOP_EVENT = threading.Event()


@dataclass(frozen=True)
class _MonitorCandidate:
    pid: int
    create_time: float | None


def _is_recovery_module_argv(argv: list[str]) -> bool:
    return any(
        value == MODULE_PATH and index > 0 and argv[index - 1] == "-m"
        for index, value in enumerate(argv)
    )


def _process_value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _monitor_candidate(
    proc: Any,
    *,
    name: str,
    config_loader: Callable[[str], Any] = load_config,
) -> _MonitorCandidate | None:
    """Identify one exact recovery observer for ``name`` across spec revisions."""
    try:
        info = proc.info
        argv = [str(value) for value in (info.get("cmdline") or ())]
        if not _is_recovery_module_argv(argv):
            return None
        declared_name = _process_value(argv, "--name")
        if declared_name is None:
            # Compatibility with observers launched before --name existed.  The
            # authored spec is the only durable identity available to them.
            authored_path = _process_value(argv, "--config-path")
            if not authored_path or config_loader(authored_path).name != name:
                return None
        elif declared_name != name:
            return None
        return _MonitorCandidate(
            pid=int(info["pid"]),
            create_time=(
                float(info["create_time"])
                if info.get("create_time") is not None
                else None
            ),
        )
    except Exception:  # stx-allow: fallback (reason: normal process-exit/access/spec-removal races are skipped)
        return None


def reconcile_recovery_monitors(
    *,
    name: str,
    process_iter: Callable[[], Any] | None = None,
    signal_fn: Callable[[int, int], None] = os.kill,
    sleep_fn: Callable[[float], None] = time.sleep,
    config_loader: Callable[[str], Any] = load_config,
    grace_s: float = 0.5,
) -> tuple[int, ...]:
    """Retire every identity-proven observer for an agent before its next launch.

    The scan is deliberately keyed by agent name, not config path: authority
    snapshots give each incarnation a different authored spec path, while the
    observer must remain a singleton across those incarnations.
    """
    if process_iter is None:
        import psutil

        process_iter = lambda: psutil.process_iter(  # noqa: E731
            ["pid", "cmdline", "create_time"]
        )
    initial = sorted(
        (
            candidate
            for proc in process_iter()
            if (
                (
                    candidate := _monitor_candidate(
                        proc, name=name, config_loader=config_loader
                    )
                )
                is not None
            )
        ),
        key=lambda item: item.pid,
    )
    signaled: set[int] = set()
    for candidate in initial:
        try:
            signal_fn(candidate.pid, signal.SIGTERM)
            signaled.add(candidate.pid)
        except ProcessLookupError:
            pass
    if signaled:
        sleep_fn(grace_s)
    remaining = {
        (candidate.pid, candidate.create_time)
        for proc in process_iter()
        if (
            (
                candidate := _monitor_candidate(
                    proc, name=name, config_loader=config_loader
                )
            )
            is not None
        )
    }
    killed = False
    for candidate in initial:
        if (candidate.pid, candidate.create_time) in remaining:
            try:
                signal_fn(candidate.pid, signal.SIGKILL)
                killed = True
            except ProcessLookupError:
                pass
    if killed:
        sleep_fn(0.05)
        survivors = {
            (candidate.pid, candidate.create_time)
            for proc in process_iter()
            if (
                (
                    candidate := _monitor_candidate(
                        proc, name=name, config_loader=config_loader
                    )
                )
                is not None
            )
        }
        failed = [
            candidate.pid
            for candidate in initial
            if (candidate.pid, candidate.create_time) in survivors
        ]
        if failed:
            raise RuntimeError(
                "identity-proven recovery observers survived SIGKILL: "
                + ", ".join(str(pid) for pid in failed)
            )
    for candidate in initial:
        log.info(
            "recovery observer reconcile: retired agent=%s pid=%d",
            name,
            candidate.pid,
        )
    return tuple(candidate.pid for candidate in initial)


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
    return _is_recovery_module_argv(decoded) and config_path in decoded


def recovery_command(config: AgentConfig) -> str:
    """The documented Hermes same-session provider rebind."""
    model = str(config.model or "").strip()
    engine_key = str(config.engine_key or "").strip()
    if not model or not engine_key:
        raise ValueError(
            "Hermes stale recovery requires a resolved engine model and key; "
            "refusing implicit provider identity"
        )
    provider = f"custom:sac-{engine_key}"
    return f"/model {model} --provider {provider} --session"


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
    recover: Callable[[], bool | HermesSlashReceipt],
    observe_progress: Callable[[], HermesTurnProgress] | None = None,
    observe_outcome: Callable[[int, str], HermesTurnOutcome] | None = None,
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
    if previous_fingerprint.startswith("recovered:"):
        if observe_outcome is None:
            return previous_fingerprint
        _, _, baseline_seq, epoch = previous_fingerprint.split(":", 3)
        outcome = observe_outcome(int(baseline_seq), epoch)
        if outcome.terminal_status == "complete":
            write_control_state(
                state_dir,
                {
                    "turn_admission": READY,
                    "detail": "same session completed a turn after provider recovery",
                    "observed_at": now(),
                },
            )
            return (
                f"ready:{fingerprint}:{outcome.latest_seq}:{outcome.epoch}"
            )
        if outcome.terminal_status in {"error", "interrupted"}:
            write_control_state(
                state_dir,
                {
                    "turn_admission": STALE_LATCHED,
                    "detail": "provider recovery verification turn failed",
                    "observed_at": now(),
                },
            )
            return latched_token
        return previous_fingerprint
    if previous_fingerprint.startswith("ready:"):
        if observe_outcome is None:
            return previous_fingerprint
        _, _, baseline_seq, epoch = previous_fingerprint.split(":", 3)
        outcome = observe_outcome(int(baseline_seq), epoch)
        if outcome.terminal_status is None:
            return previous_fingerprint
        if outcome.terminal_status == "complete":
            return (
                f"ready:{fingerprint}:{outcome.latest_seq}:{outcome.epoch}"
            )
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
    receipt = recover()
    if receipt:
        write_control_state(
            state_dir,
            {
                "turn_admission": RECOVERING,
                "detail": "provider healthy; same-session provider rebind submitted",
                "observed_at": stamp,
            },
        )
        if isinstance(receipt, HermesSlashReceipt):
            return (
                f"recovered:{fingerprint}:{receipt.latest_seq}:{receipt.epoch}"
            )
        return recovered_token
    return latched_token


def _stable_initial_delay(name: str) -> float:
    digest = hashlib.sha256(name.encode()).digest()
    return float(int.from_bytes(digest[:8], "big") % int(POLL_SECONDS))


def _run_monitor_loop(
    config: AgentConfig,
    *,
    runtime: Any,
    mux: Any,
    wait: Callable[[float], bool] = _STOP_EVENT.wait,
    state_dir: Path | None = None,
) -> None:
    """Observe until stopped or the owning tmux session naturally exits."""
    state_dir = state_dir or _state_dir(config)
    session = runtime.session_name(config)
    if not session:
        return
    if wait(_stable_initial_delay(config.name)):
        return
    recovered_fingerprint = ""
    while mux.exists(session):
        try:
            recovered_fingerprint = recovery_tick(
                config,
                capture=lambda: mux.capture_content(session),
                pause=lambda: bool(runtime.disable_periodic_turns(config)),
                recover=lambda: runtime.recover_turn_admission(config),
                observe_progress=lambda: runtime.observe_turn_progress(config),
                observe_outcome=lambda after_seq, expected_epoch: runtime.observe_turn_outcome(
                    config,
                    after_seq=after_seq,
                    expected_epoch=expected_epoch,
                ),
                previous_fingerprint=recovered_fingerprint,
                state_dir=state_dir,
            )
        except Exception:  # stx-allow: fallback (reason: one observation/control failure must not kill the bounded recovery monitor)
            log.exception("Hermes autonomous monitor tick failed for %s", config.name)
        if wait(POLL_SECONDS):
            return
    write_control_state(
        state_dir,
        {
            "turn_admission": READY,
            "detail": "owning Hermes TUI session ended; recovery observer stopped",
            "observed_at": time.time(),
        },
    )


def run_monitor(config: AgentConfig) -> None:
    """Run one rate-bounded observer for the life of the owning tmux session."""
    from .._runners._tmux.tmux import TmuxManager
    from .hermes_tui import HermesTuiSessionRuntime

    _run_monitor_loop(
        config,
        runtime=HermesTuiSessionRuntime(),
        mux=TmuxManager(),
    )


def start_recovery_monitor(
    config: AgentConfig, *, spawn: Callable[..., Any] = subprocess.Popen
) -> int | None:
    """Start one detached monitor for an autonomous Hermes TUI."""
    autonomous = getattr(config, "autonomous", None)
    if not getattr(autonomous, "enabled", False):
        return None
    stop_recovery_monitor(config)
    reconcile_recovery_monitors(name=config.name)
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
            recovery_monitor_argv(config),
            stdout=stream,
            stderr=stream,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        stream.close()
        log.warning(
            "Hermes recovery monitor failed to start for %r: %s", config.name, exc
        )
        return None
    stream.close()
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        _pid_path(config).write_text(f"{pid}\n", encoding="utf-8")
        return pid
    return None


def recovery_monitor_argv(config: AgentConfig) -> list[str]:
    """Build the detached observer command with explicit agent identity."""
    return [
        sys.executable,
        "-m",
        MODULE_PATH,
        "--name",
        config.name,
        "--config-path",
        str(getattr(config, "config_path", "") or ""),
    ]


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
    parser.add_argument("--name", required=True)
    parser.add_argument("--config-path", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config_path)
    if args.name != config.name:
        parser.error("--name must match the authored agent spec")
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
    "recovery_monitor_argv",
    "recovery_tick",
    "reconcile_recovery_monitors",
    "stale_latch",
    "start_recovery_monitor",
    "stop_recovery_monitor",
]
