"""Gateway-state projection shared by the Hermes TUI owner and its readers.

The owner publishes one ready gateway generation (port, pids, owner
evidence) atomically under a lock, waits for authenticated readiness, records
its supervision state, and removes only its own generation's files.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .._runners._hermes_owned_session import gateway_owner_evidence

GATEWAY_FILE = "hermes-tui-gateway.json"
READY_FILE = "hermes-tui-gateway.ready.json"
GATEWAY_LOCK_FILE = "hermes-tui-gateway.lock"
SUPERVISION_FILE = "hermes-tui-supervision.json"


def _wait_for_port(
    path: Path, process: subprocess.Popen, timeout_s: float = 300.0
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
    value.update(
        gateway_owner_evidence(
            state_dir, owner_pid=os.getpid(), gateway_pid=gateway_pid
        )
    )
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


__all__ = [
    "GATEWAY_FILE",
    "GATEWAY_LOCK_FILE",
    "READY_FILE",
    "SUPERVISION_FILE",
    "_atomic_json",
    "_gateway_state_lock",
    "_publish_gateway_state",
    "_remove_owned_gateway_state",
    "_select_owned_session",
    "_terminate",
    "_unlink_if_generation",
    "_wait_for_port",
    "_wait_for_readiness",
    "_write_supervision",
]
