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
import shutil
import time
from pathlib import Path

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
    tmp = state_dir / "started_at.tmp"
    tmp.write_text(repr(float(started_at)), encoding="utf-8")
    tmp.replace(state_dir / "started_at")
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


def _heartbeat_usage_fields(state_dir: Path, now: float) -> dict:
    """Build the elapsed-time + token-usage enrichment for a heartbeat.

    Sourced PROGRAMMATICALLY from the runner's own state dir — no TUI
    scraping:

      * ``elapsed_s`` from the persisted ``started_at`` (None until the
        runner has written it, so legacy / pre-start callers stay clean).
      * ``input_tokens`` / ``output_tokens`` / ``total_tokens`` from the
        accumulated ``quota.json`` (the same totals ``accumulate_quota``
        sums from each ``ResultMessage.usage``). ``total_tokens`` adds the
        cache tokens so it reflects everything billed against the session.

    Returns only the keys it can populate; ``write_heartbeat`` splats
    them onto the payload so ``elapsed_s`` is absent (not 0) when the
    start time is unknown.
    """
    out: dict = {}
    started_at = read_started_at(state_dir)
    if started_at is not None:
        out["started_at"] = started_at
        out["elapsed_s"] = round(max(0.0, now - started_at), 3)
    quota = read_quota(state_dir)
    input_tokens = int(quota.get("input_tokens", 0) or 0)
    output_tokens = int(quota.get("output_tokens", 0) or 0)
    cache_creation = int(quota.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(quota.get("cache_read_input_tokens", 0) or 0)
    out["input_tokens"] = input_tokens
    out["output_tokens"] = output_tokens
    out["total_tokens"] = input_tokens + output_tokens + cache_creation + cache_read
    return out


# /tmp pressure probe path. The session runner executes inside the
# container, where /tmp is the RAM-backed tmpfs (apptainer --containall
# default, unbounded by sac). Heavy run_in_background Bash sessions
# write per-command + task-output files there; once it fills, every
# shell command that needs a temp file fails with exit 1 + empty stdout
# — the silent "Class B" bash wedge (2026-05-22 diagnosis §3). Surfacing
# the fill % on the heartbeat turns that silent failure into an
# observable one the operator (and `sac agents status`) can see BEFORE
# the wedge.
_TMP_PRESSURE_PATH = "/tmp"  # noqa: S108 — container tmpfs, intentional


def _tmp_pressure_fields(probe_path: str = _TMP_PRESSURE_PATH) -> dict:
    """Return ``{tmp_used_pct}`` for the container tmpfs, best-effort.

    ``tmp_used_pct`` is the percentage of ``probe_path`` consumed
    (``used / total * 100``, rounded to 1 dp). Any failure — the path
    not existing (running on the host where there is no container
    ``/tmp`` tmpfs), a permission error, or a zero-total stat — degrades
    to an EMPTY dict so the heartbeat loop never crashes and the field
    is simply ABSENT rather than a misleading 0. Absent ≠ 0%: a reader
    distinguishes "not probed" from "empty tmpfs".
    """
    try:
        usage = shutil.disk_usage(probe_path)
    except OSError:
        return {}
    if usage.total <= 0:
        return {}
    return {"tmp_used_pct": round(usage.used / usage.total * 100.0, 1)}


def write_heartbeat(
    state_dir: Path,
    *,
    pid: int,
    state: str,
    name: str | None = None,
    host: str | None = None,
    ts: float | None = None,
    db_writer=None,
) -> None:
    """Atomically write the heartbeat record to ``heartbeat.json``
    AND append a row to ``state.db.heartbeats`` (diary).

    The record carries ``{ts, pid, state}`` plus, when the runner has
    recorded a start time, an ``elapsed_s`` (seconds since session
    start, derived from the persisted ``started_at``) and the running
    token totals (``input_tokens`` / ``output_tokens`` / ``total_tokens``)
    accumulated from each ``ResultMessage.usage`` into ``quota.json``.
    This lets the operator see, per agent, how long it has been running
    and how many tokens it has used — straight off the fast-path JSON.

    ``ts`` overrides the recorded heartbeat timestamp (unix seconds);
    when ``None`` (the SDK-runner default) the current wall-clock is
    used. The TUI heartbeat writer passes the agent's tmux pane-activity
    epoch here so ``heartbeat_at`` reflects the SAME liveness signal
    ``TuiSessionRuntime.is_running`` keys off (rather than the moment
    the centralized loop happened to observe it).

    When the container tmpfs is probeable it also carries
    ``tmp_used_pct`` — the ``/tmp`` fill percentage — so a filling
    tmpfs (the silent "Class B" bash-wedge precursor) is observable
    on every beat BEFORE it wedges the SDK's Bash tool. Absent (not 0)
    when the probe fails, e.g. on the host where there is no container
    ``/tmp`` tmpfs.

    The JSON file is kept as a fast-path cache for local readers
    (``sac agent status`` polls it without opening sqlite); the DB
    row is the cross-host queryable record.

    The DB write is suppressed when ``name`` or ``host`` is None —
    the diary schema requires both. Legacy callers that don't yet
    pass these stay JSON-only, no surprise rows.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    # ``now`` drives the duration-based enrichment (elapsed_s, jsonl
    # delta-bytes) so those stay honest wall-clock measurements; only
    # the recorded ``ts`` is overridable so the TUI writer can stamp the
    # actual pane-activity epoch (the liveness signal it observed).
    beat_ts = float(ts) if ts is not None else now
    payload = {"ts": beat_ts, "pid": pid, "state": state}
    payload.update(_heartbeat_usage_fields(state_dir, now))
    payload.update(_tmp_pressure_fields())
    # Operator-requested (feedback_sac_heartbeat_observability):
    # surface session.jsonl movement next to liveness so one read
    # answers "alive AND producing?". Extracted helper — see
    # ``_heartbeat_fields`` for the field semantics + the subagent
    # caveat (active subagents write to a SUBAGENT jsonl, so delta=0
    # on the main beat is a false-idle).
    # ``heartbeat_progress_fields`` adds ``capped`` (bool) +
    # ``current_phase`` (str) for card sac-heartbeat-progress-signal
    # so ``sac agents list`` can color CAPPED + board v3 dot strip
    # flips green→amber/red without scraping session.jsonl downstream.
    from ._heartbeat_fields import heartbeat_jsonl_fields, heartbeat_progress_fields

    payload.update(heartbeat_jsonl_fields(state_dir, now))
    payload.update(heartbeat_progress_fields(state_dir))
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

    def _beat() -> None:
        # Heartbeat is BEST-EFFORT: a transient state.db / FS I/O hiccup
        # (e.g. sqlite "disk I/O error" on GPFS) must NOT crash a live
        # agent. cohort-A Qwen de-risk 2026-06-23: such an error in the
        # heartbeat write propagated through ``await hb_task`` and failed
        # an ALREADY-COMPLETED solve (submission written, 8 claims
        # grounded). Log and keep beating; liveness degrades gracefully,
        # the run does not die on bookkeeping I/O.
        try:
            write_heartbeat(
                state_dir,
                pid=pid,
                state=STATE_IDLE,
                name=name,
                host=host,
                db_writer=db_writer,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort beat
            import logging

            logging.getLogger(__name__).warning(
                "heartbeat write failed (continuing, best-effort): %s", exc
            )

    _beat()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            _beat()


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
# Session id (resume marker) + append-only history
#
# Implementation extracted to ``_session_id.py`` (focused module, keeps
# this file under the 512-line cap) and imported at the top of this
# module. The names ``write_session_id`` / ``read_session_id`` /
# ``clear_session_id`` (plus the new ``append_session_id_history`` /
# ``read_session_id_history``) therefore resolve as
# ``_session_state.<name>`` unchanged for every existing caller.
# ---------------------------------------------------------------------------


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
