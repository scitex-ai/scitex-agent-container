"""State-dir layout + atomic IO helpers for the claude-session runner.

Extracted from ``_runners/claude_session.py`` so the IO surface
(``write_pid`` / ``read_pid`` / heartbeat / session id / quota /
session.jsonl) lives in one focused module that ``agent_meta``,
the runtime adapter, and the runner itself can all import without
pulling in the SDK conversation loop.

Atomic writes use the tmp + ``Path.replace`` pattern throughout so a
concurrent reader (``sac show-status``) never sees a half-formed
file.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

DEFAULT_STATE_ROOT = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
        str(Path.home() / ".scitex" / "agent-container" / "runtime"),
    )
)
DEFAULT_TICK_SECONDS = 10.0

# State-machine vocabulary used by both the runner and the runtime
# adapter's ``status`` surface. Keep tight: each value must mean exactly
# one thing to ``sac show-status`` consumers.
STATE_STARTING = "starting"
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_STOPPING = "stopping"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def state_dir_for(name: str, root: Path | None = None) -> Path:
    """Return ``<state-root>/<name>``. Does not create."""
    return (root or DEFAULT_STATE_ROOT) / name


# ---------------------------------------------------------------------------
# PID
# ---------------------------------------------------------------------------


def write_pid(state_dir: Path, pid: int) -> None:
    """Write the runner's PID atomically (tmp + rename)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "pid.tmp"
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    tmp.replace(state_dir / "pid")


def read_pid(state_dir: Path) -> int | None:
    """Return the recorded PID, or None if absent / unreadable."""
    p = state_dir / "pid"
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def write_heartbeat(state_dir: Path, *, pid: int, state: str) -> None:
    """Atomically write ``{ts, pid, state}`` to ``heartbeat.json``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "pid": pid, "state": state}
    tmp = state_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(state_dir / "heartbeat.json")


def read_heartbeat(state_dir: Path) -> dict | None:
    """Return the latest heartbeat dict, or None if absent / corrupt."""
    p = state_dir / "heartbeat.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def heartbeat_loop(
    state_dir: Path,
    *,
    pid: int,
    tick_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Write heartbeat every ``tick_seconds`` until ``stop`` is set.

    First write happens immediately so consumers see the runner alive
    without waiting a full tick.
    """
    write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            write_heartbeat(state_dir, pid=pid, state=STATE_IDLE)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def _quota_path(state_dir: Path) -> Path:
    return state_dir / "quota.json"


def read_quota(state_dir: Path) -> dict:
    """Return the persisted quota totals, or a zeroed dict if absent."""
    p = _quota_path(state_dir)
    if not p.is_file():
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def accumulate_quota(state_dir: Path, usage: dict | None) -> dict:
    """Add one ``ResultMessage.usage`` block to the running totals.

    Atomic via tmp+rename so a concurrent ``sac show-status`` reader
    never sees a partial write. Returns the new totals.
    """
    if not usage:
        return read_quota(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    totals = read_quota(state_dir)
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
    totals["turns"] = int(totals.get("turns", 0)) + 1
    tmp = state_dir / "quota.json.tmp"
    tmp.write_text(json.dumps(totals), encoding="utf-8")
    tmp.replace(_quota_path(state_dir))
    return totals


# ---------------------------------------------------------------------------
# Session id (resume marker)
# ---------------------------------------------------------------------------


def write_session_id(state_dir: Path, session_id: str) -> None:
    """Persist the SDK session id so a respawn can resume."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "session_id.tmp"
    tmp.write_text(session_id, encoding="utf-8")
    tmp.replace(state_dir / "session_id")


def read_session_id(state_dir: Path) -> str | None:
    """Return the persisted session id, or None if absent."""
    p = state_dir / "session_id"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Transcript (session.jsonl)
# ---------------------------------------------------------------------------


def append_session_message(state_dir: Path, payload: dict) -> None:
    """Append one JSON-line record to ``session.jsonl`` (with timestamp)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    enriched = {"ts": time.time(), **payload}
    with (state_dir / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(enriched, ensure_ascii=False) + "\n")
