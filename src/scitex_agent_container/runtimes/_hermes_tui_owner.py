"""Own one Hermes gateway and attach the official Ink TUI to it."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ._hermes_tui_context_owner import (
    apply_session_age_policy,
    clear_session_heartbeat,
    make_session_observer,
    reconcile_command,
    requested_session,
    resume_command,
    transition_tui_child,
)

GATEWAY_FILE = "hermes-tui-gateway.json"
READY_FILE = "hermes-tui-gateway.ready.json"
GATEWAY_LOCK_FILE = "hermes-tui-gateway.lock"
SUPERVISION_FILE = "hermes-tui-supervision.json"
POLL_SECONDS = 3.0
STARTUP_GRACE_SECONDS = 30.0
ABSENT_POLLS_BEFORE_RECOVERY = 2
MAX_RECOVERIES_PER_WINDOW = 3
RECOVERY_WINDOW_SECONDS = 300.0


def _wait_for_port(
    path: Path, process: subprocess.Popen, timeout_s: float = 30.0
) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Hermes gateway exited before ready (rc={process.returncode})"
            )
        try:
            port = int(json.loads(path.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(0.05)
            continue
        if 0 < port < 65536:
            return port
    raise RuntimeError(f"Hermes gateway did not publish {path} within {timeout_s:g}s")


def _wait_for_readiness(
    port: int,
    token: str,
    process: subprocess.Popen,
    *,
    timeout_s: float = 30.0,
    detailed_health: Callable[..., dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Wait for authenticated readiness, not the public liveness route."""
    from ._hermes_tui_rpc import HermesTuiRpcError

    if detailed_health is None:
        from ._hermes_tui_rpc import _detailed_health

        detailed_health = _detailed_health

    deadline = monotonic() + timeout_s
    last_error = "no readiness observation"
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Hermes gateway exited before ready (rc={process.returncode})"
            )
        try:
            return detailed_health(port, token, timeout_s=1.0)
        except HermesTuiRpcError as exc:
            last_error = str(exc)
            sleep(0.1)
    raise RuntimeError(
        f"Hermes gateway did not become ready within {timeout_s:g}s: {last_error}"
    )


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(value, separators=(",", ":")).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


@contextmanager
def _gateway_state_lock(state_dir: Path):
    """Serialize replacement and cleanup of the shared gateway projection."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / GATEWAY_LOCK_FILE
    with open(lock_path, "a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _publish_gateway_state(
    state_dir: Path, *, generation: str, port: int, gateway_pid: int
) -> None:
    """Atomically make one ready owner the canonical gateway generation."""
    value = {
        "generation": generation,
        "owner_pid": os.getpid(),
        "pid": gateway_pid,
        "port": port,
    }
    with _gateway_state_lock(state_dir):
        # Readers use the descriptor. Publishing it last means every visible
        # descriptor has a matching readiness projection.
        _atomic_json(state_dir / READY_FILE, value)
        _atomic_json(state_dir / GATEWAY_FILE, value)


def _unlink_if_generation(path: Path, generation: str) -> bool:
    """Unlink ``path`` only when it still names this exact owner generation."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(value, dict) or value.get("generation") != generation:
        return False
    path.unlink(missing_ok=True)
    return True


def _remove_owned_gateway_state(state_dir: Path, *, generation: str) -> None:
    """Remove canonical state without deleting a replacement owner's files."""
    with _gateway_state_lock(state_dir):
        _unlink_if_generation(state_dir / GATEWAY_FILE, generation)
        _unlink_if_generation(state_dir / READY_FILE, generation)


def _select_owned_session(
    sessions: list[dict],
    *,
    expected_identity: str,
    previous: str,
    adopt_single: bool = False,
) -> dict | None:
    """Select only the one session proven to belong to this owner."""
    if adopt_single and not previous and len(sessions) == 1:
        return sessions[0]
    identities = {expected_identity}
    if previous:
        identities.add(previous)
    matches = [
        row
        for row in sessions
        if any(row.get(field) in identities for field in ("id", "title", "session_key"))
    ]
    if len(matches) > 1 or (sessions and len(matches) != 1):
        raise RuntimeError(
            f"Hermes gateway identity mismatch for {expected_identity!r}: "
            f"{len(matches)} owned matches among {len(sessions)} live sessions"
        )
    return matches[0] if matches else None


def _terminate(process: Any, *, timeout_s: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_s)


def _write_supervision(state_dir: Path, **fields: object) -> None:
    path = state_dir / SUPERVISION_FILE
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        previous = {}
    if isinstance(previous, dict):
        comparable = {
            key: value for key, value in previous.items() if key != "observed_at"
        }
        if comparable == fields:
            return
    _atomic_json(
        path,
        {"observed_at": time.time(), **fields},
    )


def _supervise_tui(
    command: list[str],
    *,
    env: dict[str, str],
    state_dir: Path,
    gateway: Any,
    spawn: Callable[..., Any] = subprocess.Popen,
    active_list: Callable[[Path], list[dict]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_s: float = POLL_SECONDS,
    startup_grace_s: float = STARTUP_GRACE_SECONDS,
    on_spawn: Callable[[Any], None] | None = None,
    on_session_attached: Callable[[dict], None] | None = None,
    on_session_observed: Callable[[dict], str | None] | None = None,
) -> tuple[Any, int]:
    """Keep the official TUI attached to Hermes' authoritative live session.

    Hermes may detach a slow fanout peer without closing its websocket, leaving
    Ink stuck on ``computing``. It relaunches only an absent TUI child; a
    not-yet-observed continuation drops its startup query so recovery cannot
    replay a turn.
    """
    if active_list is None:
        from ._hermes_tui_rpc import active_sessions as active_list

    session_mode, expected_identity = requested_session(command)
    tui = spawn(command, env=env)
    if on_spawn is not None:
        on_spawn(tui)
    generation_started = monotonic()
    absent_polls = 0
    resume_key = ""
    recoveries: deque[float] = deque()
    attached_session_prepared = False
    _write_supervision(state_dir, state="starting", recoveries=0)

    while gateway.poll() is None and tui.poll() is None:
        try:
            sessions = active_list(state_dir)
        except Exception as exc:
            # An observation failure is not evidence that the TUI detached.
            _write_supervision(
                state_dir,
                state="observation_unavailable",
                detail=str(exc),
                recoveries=len(recoveries),
            )
            sleep(poll_s)
            continue

        try:
            owned_session = _select_owned_session(
                sessions,
                expected_identity=expected_identity,
                previous=resume_key,
                adopt_single=session_mode == "fresh",
            )
        except RuntimeError as exc:
            _write_supervision(
                state_dir,
                state="recovery_refused",
                detail=str(exc),
                active_sessions=len(sessions),
                recoveries=len(recoveries),
            )
            return tui, 70

        if owned_session is not None:
            absent_polls = 0
            resume_key = str(
                owned_session.get("session_key")
                or owned_session.get("id")
                or resume_key
            ).strip()
            if not attached_session_prepared and on_session_attached is not None:
                try:
                    on_session_attached(owned_session)
                except Exception as exc:
                    # The session stays live, but it is not declared attached
                    # until its model-calling periodic wakeup is proven absent.
                    # Retry on the next bounded observer tick.
                    _write_supervision(
                        state_dir,
                        state="periodic_turn_disable_unavailable",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        stored_session_id=resume_key,
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
                attached_session_prepared = True
            replacement: str | None = None
            if on_session_observed is not None:
                try:
                    replacement = on_session_observed(owned_session)
                except Exception as exc:
                    _write_supervision(
                        state_dir,
                        state="context_gc_refused",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        stored_session_id=resume_key,
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
            if replacement is not None:
                _write_supervision(
                    state_dir,
                    state="context_gc_transition",
                    detail=(
                        "fresh session proven from durable handoff"
                        if replacement
                        else "task completed; starting next task fresh"
                    ),
                    recoveries=len(recoveries),
                )
                (
                    tui,
                    command,
                    session_mode,
                    expected_identity,
                    resume_key,
                ) = transition_tui_child(
                    tui=tui,
                    command=command,
                    replacement=replacement,
                    env=env,
                    spawn=spawn,
                    terminate=_terminate,
                    on_spawn=on_spawn,
                )
                generation_started = monotonic()
                absent_polls = 0
                attached_session_prepared = False
                sleep(poll_s)
                continue
            _write_supervision(
                state_dir,
                state="attached",
                active_sessions=len(sessions),
                stored_session_id=resume_key,
                periodic_turns="disabled",
                recoveries=len(recoveries),
            )
            sleep(poll_s)
            continue

        outside_grace = monotonic() - generation_started >= startup_grace_s
        if outside_grace:
            absent_polls += 1
        if absent_polls < ABSENT_POLLS_BEFORE_RECOVERY:
            sleep(poll_s)
            continue

        now = monotonic()
        while recoveries and now - recoveries[0] > RECOVERY_WINDOW_SECONDS:
            recoveries.popleft()
        if (not expected_identity and not resume_key) or len(
            recoveries
        ) >= MAX_RECOVERIES_PER_WINDOW:
            _write_supervision(
                state_dir,
                state="recovery_refused",
                detail=(
                    "a fresh TUI created no session, so recovery cannot replay "
                    "its startup turn safely"
                    if not expected_identity and not resume_key
                    else "TUI transport recovery budget exhausted"
                ),
                recoveries=len(recoveries),
            )
            return tui, 70

        recoveries.append(now)
        _write_supervision(
            state_dir,
            state="recovering",
            detail=(
                "configured Hermes session is absent; reconciling official TUI "
                "without replaying its startup turn"
            ),
            **({"stored_session_id": resume_key} if resume_key else {}),
            recoveries=len(recoveries),
        )
        _terminate(tui)
        recovery_command = (
            resume_command(command, resume_key)
            if resume_key
            else reconcile_command(command)
        )
        tui = spawn(recovery_command, env=env)
        if on_spawn is not None:
            on_spawn(tui)
        generation_started = monotonic()
        absent_polls = 0
        attached_session_prepared = False

    return tui, int(tui.poll() or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sac-hermes-tui-owner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--max-session-age-minutes", type=int, default=4320)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a Hermes TUI command is required after --")

    state_dir = Path(args.state_dir)
    generation = uuid.uuid4().hex
    gateway_ready_path = state_dir / f".{READY_FILE}.{generation}"
    token_path = state_dir / "hermes-api.key"
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise RuntimeError(f"missing or invalid Hermes gateway key: {token_path}")
    gateway_ready_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["HERMES_DASHBOARD_SESSION_TOKEN"] = token
    env["HERMES_DESKTOP_READY_FILE"] = str(gateway_ready_path)
    gateway = subprocess.Popen(
        ["hermes", "serve", "--host", "127.0.0.1", "--port", "0", "--isolated"],
        env=env,
    )
    tui: subprocess.Popen | None = None

    def remember_tui(process: subprocess.Popen) -> None:
        nonlocal tui
        tui = process

    def forward(signum: int, _frame: object) -> None:
        if tui is not None and tui.poll() is None:
            tui.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        port = _wait_for_port(gateway_ready_path, gateway)
        _wait_for_readiness(port, token, gateway)
        _publish_gateway_state(
            state_dir,
            generation=generation,
            port=port,
            gateway_pid=gateway.pid,
        )
        tui_env = os.environ.copy()
        tui_env["HERMES_TUI_GATEWAY_URL"] = (
            f"ws://127.0.0.1:{port}/api/ws?token={token}"
        )
        from ._hermes_context_rpc import stored_session_for_identity

        command = apply_session_age_policy(
            command,
            state_dir=state_dir,
            max_age_minutes=args.max_session_age_minutes,
            stored_lookup=stored_session_for_identity,
        )

        tui, result = _supervise_tui(
            command,
            env=tui_env,
            state_dir=state_dir,
            gateway=gateway,
            on_spawn=remember_tui,
            on_session_attached=lambda session: clear_session_heartbeat(
                state_dir, session
            ),
            on_session_observed=make_session_observer(state_dir, command),
        )
        return result
    finally:
        _remove_owned_gateway_state(state_dir, generation=generation)
        gateway_ready_path.unlink(missing_ok=True)
        _write_supervision(state_dir, state="stopped")
        if tui is not None and tui.poll() is None:
            tui.terminate()
            try:
                tui.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tui.kill()
        if gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["GATEWAY_FILE", "READY_FILE", "main"]
