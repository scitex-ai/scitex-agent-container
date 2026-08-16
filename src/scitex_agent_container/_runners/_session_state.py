"""State-dir layout + atomic IO helpers for the claude-session runner.

Extracted from ``_runners/claude_session.py`` so the IO surface
(``write_pid`` / ``read_pid`` / heartbeat / session id / quota /
session.jsonl) lives in one focused module that ``agent_meta``,
the runtime adapter, and the runner itself can all import without
pulling in the SDK conversation loop.

Atomic writes use the tmp + ``Path.replace`` pattern throughout so a
concurrent reader (``sac agent status``) never sees a half-formed
file.

The heartbeat surface (payload construction, diary-DB forwarding, the
periodic loop, and the state vocabulary) lives in :mod:`._session_beat`
(extracted under the 512-line cap when the v4 step-5 liveness artifact
landed) and is re-exported here — every existing
``_session_state.write_heartbeat`` / ``.heartbeat_loop`` /
``.STATE_*`` importer keeps working unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# SSOT: the runtime base (``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` knob or
# the ``~/.scitex/agent-container/runtime`` default) is resolved once in
# ``_runtime_paths.runtime_base_dir`` so state.db / registry / this
# per-agent state root all relocate together.
from .._runtime_paths import runtime_base_dir

# Per-writer-unique atomic text write: a UNIQUE tmp name so two processes
# sharing one state dir never collide on a fixed ``<name>.tmp`` sibling
# (the ``instance_id.tmp`` FileNotFoundError race). See ``_atomic``.
from ._atomic import atomic_write_text

# Heartbeat surface — extracted to ``_session_beat`` (v4 step 5, line
# cap); re-exported with explicit ``as`` aliases marking the intentional
# re-export so every existing importer keeps resolving.
from ._session_beat import STATE_BUSY as STATE_BUSY
from ._session_beat import STATE_IDLE as STATE_IDLE
from ._session_beat import STATE_READY as STATE_READY
from ._session_beat import STATE_STARTING as STATE_STARTING
from ._session_beat import STATE_STOPPING as STATE_STOPPING
from ._session_beat import STATE_WORKING as STATE_WORKING
from ._session_beat import _DefaultDBWriter as _DefaultDBWriter
from ._session_beat import _heartbeat_usage_fields as _heartbeat_usage_fields
from ._session_beat import _resolve_db_writer as _resolve_db_writer
from ._session_beat import _tmp_pressure_fields as _tmp_pressure_fields
from ._session_beat import heartbeat_loop as heartbeat_loop
from ._session_beat import read_heartbeat as read_heartbeat
from ._session_beat import record_turn_transition as record_turn_transition
from ._session_beat import report_sdk_error as report_sdk_error
from ._session_beat import write_heartbeat as write_heartbeat

# Session-id resume marker + append-only history live in a focused
# module to keep this file under the 512-line cap. Re-exported here
# (explicit ``as`` aliases mark the intentional re-export) so the
# existing importers of ``_session_state.{write,read,clear}_session_id``
# keep working unchanged.
from ._session_id import append_session_id_history as append_session_id_history
from ._session_id import clear_session_history as clear_session_history
from ._session_id import clear_session_id as clear_session_id
from ._session_id import discard_dead_session as discard_dead_session
from ._session_id import read_session_id as read_session_id
from ._session_id import read_session_id_history as read_session_id_history
from ._session_id import write_session_id as write_session_id

# Quota totals live in a focused module (keeps this file under the
# 512-line cap). Re-exported so ``_session_state.{read,accumulate}_quota``
# importers keep working unchanged.
from ._session_quota import accumulate_quota as accumulate_quota
from ._session_quota import read_quota as read_quota

DEFAULT_STATE_ROOT = runtime_base_dir()
DEFAULT_TICK_SECONDS = 10.0


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
    atomic_write_text(state_dir / "pid", f"{pid}\n")


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
# Session start time (for heartbeat elapsed_s)
# ---------------------------------------------------------------------------


def write_started_at(state_dir: Path, started_at: float | None = None) -> float:
    """Persist the runner's session start time (unix seconds) atomically.

    Written once at runner startup so every heartbeat can report
    ``elapsed_s`` without the heartbeat loop having to carry the
    start time in memory (it survives a supervised restart of the
    conversation task too, since the file outlives it). Returns the
    value written so the caller can reuse it.
    """
    if started_at is None:
        started_at = time.time()
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_dir / "started_at", repr(float(started_at)))
    return float(started_at)


def read_started_at(state_dir: Path) -> float | None:
    """Return the persisted session start time, or None if absent / corrupt."""
    p = state_dir / "started_at"
    if not p.is_file():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# state.db instance id (F-CS11 phase 3)
#
# The instance id is a uuid7 generated at start time by
# ``_state.state_db.record_instance_start``. The runtime persists it
# in ``<state_dir>/instance_id`` so the stop path can resolve the
# row in ``state.db.instances`` without rescanning by name+host.
# v4 step 5: this uuid IS the INCARNATION ID — the runner process
# adopts it (bind-once; see ``_incarnation``) and stamps it on its own
# beats, the birth certificate keys on it, and the terminal ExitRecord
# cites it.
# ---------------------------------------------------------------------------


def write_instance_id(state_dir: Path, instance_id: str) -> None:
    """Persist the state.db instance uuid alongside the runner pid."""
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_dir / "instance_id", instance_id)


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
