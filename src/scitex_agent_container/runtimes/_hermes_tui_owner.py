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


def _requested_session(command: list[str]) -> tuple[str, str]:
    """Return SAC's requested Hermes session mode and stable identity."""
    for index, value in enumerate(command[:-1]):
        if value in {"--continue", "-c"}:
            return "continue", str(command[index + 1]).strip()
        if value in {"--resume", "-r"}:
            return "resume", str(command[index + 1]).strip()
    return "fresh", ""


def _consume_fork_seed(
    state_dir: Path,
    command: list[str],
    *,
    import_fn: Callable[[Path, dict], str] | None = None,
) -> bool:
    """Import one owner-only Hermes fork handoff before spawning the TUI."""
    from .._lifecycle._twin import HERMES_FORK_SEED_FILE

    path = state_dir / HERMES_FORK_SEED_FILE
    if not os.path.lexists(path):
        return False
    if path.is_symlink():
        raise RuntimeError(f"Hermes fork seed must not be a symlink: {path}")
    stat_result = path.stat()
    if not path.is_file() or stat_result.st_uid != os.geteuid():
        raise RuntimeError("Hermes fork seed must be an owner-controlled regular file")
    if stat_result.st_mode & 0o077:
        raise RuntimeError("Hermes fork seed permissions must be 0600")
    if stat_result.st_size > 64 * 1024 * 1024:
        raise RuntimeError("Hermes fork seed exceeds the 64 MiB safety limit")
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Hermes fork seed is unreadable: {exc}") from exc
    mode, expected_identity = _requested_session(command)
    title = str(seed.get("title") or "").strip() if isinstance(seed, dict) else ""
    if mode != "continue" or not expected_identity or title != expected_identity:
        raise RuntimeError(
            "Hermes fork seed identity does not match the requested continuation"
        )
    if import_fn is None:
        from ._hermes_tui_rpc import import_fork_seed as import_fn

    stored_id = import_fn(state_dir, seed)
    if not str(stored_id or "").strip():
        raise RuntimeError("Hermes fork seed import returned no stored session id")
    path.unlink()
    return True


def _resume_command(command: list[str], stored_session_id: str) -> list[str]:
    """Resume one exact transcript without replaying the startup task."""
    rebuilt: list[str] = []
    skip_value = False
    value_flags = {"--continue", "-c", "--resume", "-r", "--query"}
    for value in command:
        if skip_value:
            skip_value = False
            continue
        if value in value_flags:
            skip_value = True
            continue
        if value == "--create-if-missing":
            continue
        rebuilt.append(value)
    return rebuilt + ["--resume", stored_session_id]


def _reconcile_command(command: list[str]) -> list[str]:
    """Re-open the named session without replaying its startup turn."""
    rebuilt: list[str] = []
    skip_value = False
    for value in command:
        if skip_value:
            skip_value = False
            continue
        if value in {"--query", "--query-file", "-q"}:
            skip_value = True
            continue
        rebuilt.append(value)
    return rebuilt


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


def _clear_session_heartbeat(state_dir: Path, session: dict) -> None:
    """Remove persisted periodic model wakeup for one proven live session.

    SAC's durable Cards/CCT/SAC ingress owns wakeup.  A Hermes heartbeat does
    a full-context model turn merely to discover whether work exists, so the
    owner removes legacy heartbeat state before it declares the TUI attached.
    """
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        raise RuntimeError("owned Hermes session has no id")
    from ._hermes_tui_rpc import clear_heartbeat_for_session

    clear_heartbeat_for_session(state_dir, session_id)


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
) -> tuple[Any, int]:
    """Keep the official TUI attached to Hermes' authoritative live session.

    Hermes may detach a slow fanout peer without closing its websocket.  The
    Ink client consequently cannot run its own close-triggered reconnect and
    can display ``computing`` forever after the backend has completed.  This
    owner watches ``session.active_list`` through a short-lived, non-viewer RPC
    and relaunches only the TUI child when its configured session is absent.
    An observed durable session id is resumed exactly.  Before any id has been
    observed, the stable named continuation is reconciled with its startup
    query removed, so recovery cannot replay a turn.
    """
    if active_list is None:
        from ._hermes_tui_rpc import active_sessions as active_list

    session_mode, expected_identity = _requested_session(command)
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
            _resume_command(command, resume_key)
            if resume_key
            else _reconcile_command(command)
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
        # A fork handoff is imported only after this child's authenticated
        # gateway is ready and before the official TUI resolves --continue.
        # The seed is deleted by _consume_fork_seed only after Hermes confirms
        # the native session.create/session.branch import.
        _consume_fork_seed(state_dir, command)
        tui_env = os.environ.copy()
        tui_env["HERMES_TUI_GATEWAY_URL"] = (
            f"ws://127.0.0.1:{port}/api/ws?token={token}"
        )
        tui, result = _supervise_tui(
            command,
            env=tui_env,
            state_dir=state_dir,
            gateway=gateway,
            on_spawn=remember_tui,
            on_session_attached=lambda session: _clear_session_heartbeat(
                state_dir, session
            ),
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
