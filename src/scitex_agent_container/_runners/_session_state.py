"""State-dir layout + atomic IO helpers for the claude-session runner.

Extracted from ``_runners/claude_session.py`` so the IO surface
(``write_pid`` / ``read_pid`` / heartbeat / session id / quota /
session.jsonl) lives in one focused module that ``agent_meta``,
the runtime adapter, and the runner itself can all import without
pulling in the SDK conversation loop.

Atomic writes use the tmp + ``Path.replace`` pattern throughout so a
concurrent reader (``sac agent status``) never sees a half-formed
file.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
# one thing to ``sac agent status`` consumers.
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


class _DefaultDBWriter:
    """Production writer that forwards to ``_state.state_db_diary``.

    Imports lazily so test environments (which may not have the
    container venv fully wired) don't pay the import cost. The
    diary writes are best-effort: a single-table failure must not
    crash the runner's heartbeat loop, so we catch + log here.
    """

    def __init__(self) -> None:
        self._log = logging.getLogger(__name__ + "._DefaultDBWriter")

    def record_heartbeat(self, **kwargs):
        from .._state.state_db_diary import record_heartbeat

        return record_heartbeat(**kwargs)

    def record_turn(self, **kwargs):
        from .._state.state_db_diary import record_turn

        return record_turn(**kwargs)

    def record_error(self, **kwargs):
        from .._state.state_db_diary import record_error

        return record_error(**kwargs)


def _resolve_db_writer(db_writer):
    """Return the injected writer or a freshly-built default.

    Centralised so every runner entry point uses the same fallback
    rule. No silent fallbacks: if the caller passes ``None`` we
    build a real writer; if they pass an object we use it as-is.
    """
    return db_writer if db_writer is not None else _DefaultDBWriter()


def write_heartbeat(
    state_dir: Path,
    *,
    pid: int,
    state: str,
    name: str | None = None,
    host: str | None = None,
    db_writer=None,
) -> None:
    """Atomically write ``{ts, pid, state}`` to ``heartbeat.json``
    AND append a row to ``state.db.heartbeats`` (diary).

    The JSON file is kept as a fast-path cache for local readers
    (``sac agent status`` polls it without opening sqlite); the DB
    row is the cross-host queryable record.

    The DB write is suppressed when ``name`` or ``host`` is None —
    the diary schema requires both. Legacy callers that don't yet
    pass these stay JSON-only, no surprise rows.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "pid": pid, "state": state}
    tmp = state_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(state_dir / "heartbeat.json")
    if name and host:
        writer = _resolve_db_writer(db_writer)
        writer.record_heartbeat(
            name=name, host=host, pid=pid, state=state, ts=payload["ts"]
        )


def report_sdk_error(
    *,
    name: str,
    host: str,
    cause: str,
    detail: str | None = None,
    turn_id: str | None = None,
    db_writer=None,
) -> int:
    """Append one row to ``state.db.errors`` describing a runner crash.

    Returns the new ``error_id``. ``cause`` is a short identifier
    (``auth`` / ``network`` / ``sdk-crash`` / ``schema-mismatch``
    / ...) that the lead groups on; ``detail`` carries the longer
    message or traceback.
    """
    writer = _resolve_db_writer(db_writer)
    return writer.record_error(
        name=name, host=host, cause=cause, detail=detail, turn_id=turn_id
    )


def record_turn_transition(
    *,
    turn_id: str,
    name: str,
    host: str,
    status: str,
    prompt_text: str | None = None,
    response_text: str | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    db_writer=None,
) -> None:
    """Append one row to ``state.db.turns`` for a turn state-transition.

    A successful turn produces four rows sharing the same
    ``turn_id``: ``queued`` → ``delivered`` → ``read`` →
    ``responded``. Errors append a fifth row with status
    ``error`` and a paired ``state.db.errors`` row (see
    :func:`report_sdk_error`).
    """
    writer = _resolve_db_writer(db_writer)
    writer.record_turn(
        turn_id=turn_id,
        name=name,
        host=host,
        status=status,
        prompt_text=prompt_text,
        response_text=response_text,
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


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
    name: str | None = None,
    host: str | None = None,
    db_writer=None,
) -> None:
    """Write heartbeat every ``tick_seconds`` until ``stop`` is set.

    First write happens immediately so consumers see the runner alive
    without waiting a full tick. When ``name`` and ``host`` are
    supplied each beat also appends a row to ``state.db.heartbeats``
    (the diary table) so the lead can query cross-host state without
    walking heartbeat.json files. Legacy callers that omit the pair
    stay JSON-only.
    """
    write_heartbeat(
        state_dir, pid=pid, state=STATE_IDLE, name=name, host=host, db_writer=db_writer
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            write_heartbeat(
                state_dir,
                pid=pid,
                state=STATE_IDLE,
                name=name,
                host=host,
                db_writer=db_writer,
            )


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

    Atomic via tmp+rename so a concurrent ``sac agent status`` reader
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


def clear_session_id(state_dir: Path) -> bool:
    """Remove the persisted ``session_id`` resume marker.

    Used by ``agent_start(force=True)`` so a stale session id left over
    from a previous run can't make the SDK try to resume a conversation
    the server has already aged out (symptom: ``ProcessError: Command
    failed with exit code 1`` ~90s into the first turn).

    Returns True if a file was removed, False if there was nothing to
    remove. Never raises FileNotFoundError; never silently swallows
    other ``OSError``s (callers want a loud failure if e.g. the runtime
    dir is unreadable due to permissions).
    """
    p = state_dir / "session_id"
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# state.db instance id (F-CS11 phase 3)
#
# The instance id is a uuid7 generated at start time by
# ``_state.state_db.record_instance_start``. The runtime persists it
# in ``<state_dir>/instance_id`` so the stop path can resolve the
# row in ``state.db.instances`` without rescanning by name+host.
# ---------------------------------------------------------------------------


def write_instance_id(state_dir: Path, instance_id: str) -> None:
    """Persist the state.db instance uuid alongside the runner pid."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "instance_id.tmp"
    tmp.write_text(instance_id, encoding="utf-8")
    tmp.replace(state_dir / "instance_id")


def read_instance_id(state_dir: Path) -> str | None:
    """Return the persisted state.db instance uuid, or None if absent."""
    p = state_dir / "instance_id"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def clear_instance_id(state_dir: Path) -> None:
    """Remove the persisted instance id file (called from stop)."""
    p = state_dir / "instance_id"
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Transcript (session.jsonl)
# ---------------------------------------------------------------------------


def append_session_message(state_dir: Path, payload: dict) -> None:
    """Append one JSON-line record to ``session.jsonl`` (with timestamp)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    enriched = {"ts": time.time(), **payload}
    with (state_dir / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(enriched, ensure_ascii=False) + "\n")
