"""Heartbeat write/read surface for the session runner state dir.

Extracted from ``_session_state.py`` (which hit the 512-line cap when
the v4 step-5 liveness artifact landed) following the same convention as
``_session_id`` / ``_session_quota``: this module owns the heartbeat
payload construction, the diary-DB forwarding, and the periodic loop;
``_session_state`` re-exports every public name so existing importers
(``runner.write_heartbeat`` etc.) keep their call shapes unchanged.

The v4 step-5 beat (card sac-v4-layering-refactor-harness-runtime-
inference-20260813) carries, additively::

    {ts, pid, state, seq, writer, incarnation_id?, turns_completed, ...}

``seq`` is monotonic per heartbeat.json; ``writer`` names who wrote the
beat; ``incarnation_id`` appears only on SELF-testimony beats (the
process bound its own incarnation — see ``_incarnation``); the resident
``state`` vocabulary is ``starting | busy | ready | stopping`` (legacy
``idle`` / ``working`` still readable from pre-upgrade beats).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from ._atomic import atomic_write_text
from ._session_quota import read_quota

logger = logging.getLogger(__name__)

# State-machine vocabulary used by both the runner and the runtime
# adapter's ``status`` surface. Keep tight: each value must mean exactly
# one thing to ``sac agent status`` consumers. v4 step 5 splits the
# resident vocabulary into READY (the daemon CAN consume its inbox —
# conversation task alive) and BUSY (a turn is in flight); IDLE/WORKING
# remain defined for readers of pre-upgrade beats but are no longer
# written by the daemon or the turn driver.
STATE_STARTING = "starting"
STATE_IDLE = "idle"  # legacy (pre-v4-step-5 beats); superseded by READY
STATE_WORKING = "working"  # legacy (pre-v4-step-5 beats); superseded by BUSY
STATE_READY = "ready"
STATE_BUSY = "busy"
STATE_STOPPING = "stopping"


# Consecutive diary-write failures, keyed by row kind. MODULE level on
# purpose: ``_resolve_db_writer`` builds a FRESH ``_DefaultDBWriter`` on
# every call, so a per-instance counter would reset each beat and warn on
# every tick. Keyed by kind so a broken heartbeat table cannot mute the
# first turn failure.
_DIARY_FAILURES: dict[str, int] = {}

# Warn on the 1st failure of a kind, then every Nth. The diary is
# best-effort, but a diary that is silently unreachable is precisely how
# the store-DSN defect ran unnoticed from 08-23 to 08-27 — so a degraded
# diary stays visible in the log without flooding a per-tick loop.
_DIARY_WARN_EVERY = 100


class _DefaultDBWriter:
    """Production writer that forwards to ``_state.state_db_diary``.

    Imports lazily so test environments (which may not have the
    container venv fully wired) don't pay the import cost. The
    diary writes are best-effort: a single-table failure must not
    crash the runner's heartbeat loop, so we catch + log here.

    Best-effort is the CONTRACT, not a convenience. The diary is
    observational — turns, errors, heartbeats. An agent whose telemetry
    store is unreachable must keep running and lose rows, never die with
    it. That sentence was in this docstring before any of it was true:
    the SQLite diary wrote to a local file that effectively never failed,
    so the promised catch was never exercised and never implemented. A
    remote store makes the failure real, and every runner test that
    merely ticks a heartbeat died on a refused connection. The catch is
    now actually here.

    What this does NOT do is fail quietly. Every failing kind is logged
    with its CONSECUTIVE count, so "the diary has been down for 4000
    beats" is readable in the log rather than inferred from missing rows
    — and recovery is logged too, so the end of an outage has a
    timestamp.
    """

    def __init__(self) -> None:
        self._log = logging.getLogger(__name__ + "._DefaultDBWriter")

    def _best_effort(self, kind: str, write):
        """Run one diary write, absorbing any failure into a log line.

        ``write`` is a thunk that performs its own lazy import, so an
        ImportError from a half-wired environment is caught on the same
        path as a refused Postgres connection — both mean "no row", and
        neither is the runner's problem to die of.
        """
        try:
            result = write()
        except Exception as exc:  # noqa: BLE001 - telemetry must not kill the runner
            seen = _DIARY_FAILURES.get(kind, 0) + 1
            _DIARY_FAILURES[kind] = seen
            if seen == 1 or seen % _DIARY_WARN_EVERY == 0:
                self._log.warning(
                    "diary %s write failed (%d consecutive; rows are being "
                    "dropped): %s: %s",
                    kind,
                    seen,
                    type(exc).__name__,
                    exc,
                )
            return None
        if _DIARY_FAILURES.get(kind):
            self._log.warning(
                "diary %s write recovered after %d consecutive failures",
                kind,
                _DIARY_FAILURES[kind],
            )
            _DIARY_FAILURES[kind] = 0
        return result

    def record_heartbeat(self, **kwargs):
        def _write():
            from .._state.state_db_diary import record_heartbeat

            return record_heartbeat(**kwargs)

        return self._best_effort("heartbeat", _write)

    def record_turn(self, **kwargs):
        def _write():
            from .._state.state_db_diary import record_turn

            return record_turn(**kwargs)

        return self._best_effort("turn", _write)

    def record_error(self, **kwargs):
        def _write():
            from .._state.state_db_diary import record_error

            return record_error(**kwargs)

        return self._best_effort("error", _write)


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
      * ``turns_completed`` — the completed-turn counter the same
        ``accumulate_quota`` bumps per ResultMessage (v4 step 5).

    Returns only the keys it can populate; ``write_heartbeat`` splats
    them onto the payload so ``elapsed_s`` is absent (not 0) when the
    start time is unknown.
    """
    from ._session_state import read_started_at

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
    out["turns_completed"] = int(quota.get("turns", 0) or 0)
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
    writer: str | None = None,
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

    v4 step 5 adds the liveness-artifact fields, additively: ``seq``
    (monotonic per heartbeat.json, whoever wrote it),
    ``turns_completed``, ``writer`` (who wrote this beat — see
    ``_incarnation.WRITER_*``), and ``incarnation_id`` — the last ONLY
    when THIS process bound its own incarnation (see ``_incarnation``;
    observer beats stay incarnation-less, honestly).

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
    from ._incarnation import incarnation_beat_fields

    payload.update(
        incarnation_beat_fields(
            state_dir, prev_beat=read_heartbeat(state_dir), writer=writer
        )
    )
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
    atomic_write_text(state_dir / "heartbeat.json", json.dumps(payload))
    if name and host:
        db = _resolve_db_writer(db_writer)
        db.record_heartbeat(name=name, host=host, pid=pid, state=state, ts=payload["ts"])


def report_sdk_error(
    *,
    name: str,
    host: str,
    cause: str,
    detail: str | None = None,
    turn_id: str | None = None,
    db_writer=None,
) -> int | None:
    """Append one row to ``state.db.errors`` describing a runner crash.

    Returns the new ``error_id``, or ``None`` when the diary was
    unreachable and the row was dropped — the default writer is
    best-effort (see :class:`_DefaultDBWriter`), and an error report
    that cannot be stored must not itself raise inside a crash path.
    Annotated honestly rather than papered over with a sentinel id: no
    caller reads this value today, and a fake integer would be
    indistinguishable from a row that really landed.

    ``cause`` is a short identifier (``auth`` / ``network`` /
    ``sdk-crash`` / ``schema-mismatch`` / ...) that the lead groups on;
    ``detail`` carries the longer message or traceback.
    """
    db = _resolve_db_writer(db_writer)
    return db.record_error(
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
    db = _resolve_db_writer(db_writer)
    db.record_turn(
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
    state_fn=None,
    writer: str | None = None,
) -> None:
    """Write heartbeat every ``tick_seconds`` until ``stop`` is set.

    First write happens immediately so consumers see the runner alive
    without waiting a full tick. When ``name`` and ``host`` are
    supplied each beat also appends a row to ``state.db.heartbeats``
    (the diary table) so the lead can query cross-host state without
    walking heartbeat.json files. Legacy callers that omit the pair
    stay JSON-only.

    ``state_fn`` (v4 step 5) computes the CURRENT state per beat so the
    loop can report ready/busy/stopping honestly instead of asserting a
    blanket IDLE; ``None`` keeps the legacy IDLE behaviour. ``writer``
    names this loop on every beat it writes.
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
                state=state_fn() if state_fn is not None else STATE_IDLE,
                name=name,
                host=host,
                db_writer=db_writer,
                writer=writer,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort beat
            logging.getLogger(__name__).warning(
                "heartbeat write failed (continuing, best-effort): %s", exc
            )

    _beat()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            _beat()


__all__ = [
    "STATE_BUSY",
    "STATE_IDLE",
    "STATE_READY",
    "STATE_STARTING",
    "STATE_STOPPING",
    "STATE_WORKING",
    "heartbeat_loop",
    "read_heartbeat",
    "record_turn_transition",
    "report_sdk_error",
    "write_heartbeat",
]
