"""Incarnation binding + terminal ExitRecord for the residency daemon.

v4 migration step 5 (card sac-v4-layering-refactor-harness-runtime-
inference-20260813, operator identity model settled 2026-08-14). Three
identities, one join key:

  * SPEC ID        — the design file, tracked by git.
  * AGENT ID       — the durable named subject (cards, memory, inbox).
  * INCARNATION ID — one process lifetime. Beats, the birth
    certificate and the ExitRecord all key on it.

The incarnation id IS the ``instances.id`` uuid7 the start path mints
(:func:`.._state.state_db.record_instance_start`) and persists to
``<state_dir>/instance_id``. This module owns how the RUNNER PROCESS
adopts that id and how it testifies about its own death.

ADOPTION IS BIND-ONCE, AND THAT IS THE WHOLE POINT
--------------------------------------------------
A beat that merely re-read ``instance_id`` on every tick would be an
ECHO of whatever the last start path wrote — including a start path
that minted a fresh id over a process it never actually cycled (the
P0 of 2026-08-14, where four "verified" restarts never touched the
process). The restart verdict machinery treats the beat as a SECOND
WITNESS precisely because the daemon binds the id ONCE, near its own
boot, and never rebinds: an old process keeps testifying to its OLD
incarnation no matter how many new ids the ledger mints over it.

Two guards keep the bind honest:

  * the marker file must be YOUNGER than this process (small grace for
    the write-before-boot race) — a stale marker left by a crashed
    previous incarnation is refused, the daemon simply beats without an
    incarnation until the start path writes the fresh one;
  * once bound, later rewrites of the file are ignored for the life of
    the process.

Only a process that BINDS stamps ``incarnation_id`` onto its beats.
Observer writers (the listen daemon's centralized TUI/SDK heartbeat
loops, which overwrite the same ``heartbeat.json`` from the HOST) never
bind, so their proxy beats carry no incarnation — an observer knows the
process exists, not which incarnation it is. The ``writer`` beat field
names who wrote each beat so the two are distinguishable.

THE EXIT RECORD SAYS WHY
------------------------
Beats say an agent is dead (they stop). ``exit.json`` says WHY, in a
closed vocabulary::

    {"incarnation_id": ..., "reason": ..., "code": ..., "ts": ..., "pid": ...}

``reason`` ∈ {stopped-by-signal, oneshot-complete, harness-returned,
crashed}. ``harness-returned`` is the 2026-08-14 outage shape made
visible: the conversation task ended on its own while the daemon was
supposed to stay resident. The record is also mirrored (best-effort)
onto the ``incarnations`` row in state.db so the death is queryable
next to the birth certificate.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from ._atomic import atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_CRASHED",
    "EXIT_HARNESS_RETURNED",
    "EXIT_ONESHOT_COMPLETE",
    "EXIT_RECORD_FILENAME",
    "EXIT_STOPPED_BY_SIGNAL",
    "VALID_EXIT_REASONS",
    "WRITER_SDK_OBSERVER",
    "WRITER_SESSION_DAEMON",
    "WRITER_TUI_OBSERVER",
    "WRITER_TURN_DRIVER",
    "ExitReasonHolder",
    "bound_incarnation",
    "clear_exit_record",
    "clear_incarnation_binding",
    "incarnation_beat_fields",
    "read_exit_record",
    "try_bind_incarnation",
    "write_exit_record",
]

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The daemon was asked to stop (SIGTERM/SIGINT) and complied. Code 0.
EXIT_STOPPED_BY_SIGNAL = "stopped-by-signal"
#: A deliberately finite run finished its plan: a ``--print-stream``
#: foreground mission, or the autonomous drive-until-done loop.
EXIT_ONESHOT_COMPLETE = "oneshot-complete"
#: The conversation (turn-driver) task RETURNED on its own while the
#: daemon was meant to stay resident — the residency contract violation
#: that used to leave a zombie with green heartbeats.
EXIT_HARNESS_RETURNED = "harness-returned"
#: The conversation task raised, or the daemon itself died on an
#: exception. The traceback lives in stdout.log / session.jsonl.
EXIT_CRASHED = "crashed"

VALID_EXIT_REASONS = frozenset(
    {
        EXIT_STOPPED_BY_SIGNAL,
        EXIT_ONESHOT_COMPLETE,
        EXIT_HARNESS_RETURNED,
        EXIT_CRASHED,
    }
)

#: Beat ``writer`` values — who wrote this beat. Self-testimony
#: (daemon / turn driver, in the agent's own process) is evidence about
#: the incarnation; observer beats are proxy liveness from the host.
WRITER_SESSION_DAEMON = "session-daemon"
WRITER_TURN_DRIVER = "turn-driver"
WRITER_TUI_OBSERVER = "listen-tui-observer"
WRITER_SDK_OBSERVER = "listen-sdk-observer"

EXIT_RECORD_FILENAME = "exit.json"

#: How much older than this process the ``instance_id`` marker may be
#: and still be adopted. Covers the benign race where the start path
#: writes the marker moments before the daemon's interpreter is up.
#: A marker older than this predates us — a previous incarnation's
#: leftover — and is refused.
_BIND_GRACE_S = 60.0

#: This process's boot epoch (module import happens during runner boot).
_PROCESS_BOOT_TS = time.time()

#: Bind-once cache: ``str(state_dir) -> incarnation_id``. Per-process by
#: construction (module state); the daemon clears its own entry at boot
#: so an in-process test harness running several daemons stays honest.
_BINDINGS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Incarnation binding
# ---------------------------------------------------------------------------


def try_bind_incarnation(
    state_dir: Path,
    *,
    boot_ts: float | None = None,
    grace_s: float = _BIND_GRACE_S,
) -> str | None:
    """Adopt ``<state_dir>/instance_id`` as THIS process's incarnation.

    Bind-once: the first successful read wins for the life of the
    process; later calls return the cached id without touching the file,
    so a start path that rewrites the marker over a live process cannot
    make that process testify to an incarnation it never was.

    A marker file older than ``boot_ts - grace_s`` is refused (a crashed
    previous incarnation's leftover) — the caller simply gets ``None``
    until the current start path writes the fresh marker. Never raises.
    """
    key = str(state_dir)
    if key in _BINDINGS:
        return _BINDINGS[key]
    marker = Path(state_dir) / "instance_id"
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return None
    born = _PROCESS_BOOT_TS if boot_ts is None else float(boot_ts)
    if mtime < born - grace_s:
        return None
    try:
        incarnation = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not incarnation:
        return None
    _BINDINGS[key] = incarnation
    return incarnation


def bound_incarnation(state_dir: Path) -> str | None:
    """The incarnation THIS process already bound for ``state_dir``, or None.

    Passive — never reads the marker file. This is what beat writers
    consult, so a process that never bound (an observer, a sidecar)
    never stamps an incarnation it does not own.
    """
    return _BINDINGS.get(str(state_dir))


def clear_incarnation_binding(state_dir: Path) -> None:
    """Drop the bind for ``state_dir`` (daemon boot / test isolation)."""
    _BINDINGS.pop(str(state_dir), None)


# ---------------------------------------------------------------------------
# Beat enrichment
# ---------------------------------------------------------------------------


def incarnation_beat_fields(
    state_dir: Path,
    *,
    prev_beat: dict | None,
    writer: str | None,
) -> dict:
    """The v4 liveness-artifact enrichment for one heartbeat payload.

    Additive fields only (existing consumers keep reading ``ts`` /
    ``pid`` / ``state`` untouched):

      * ``seq`` — monotonic per heartbeat.json: previous beat's seq + 1,
        whoever wrote it. A reader that sees seq stop advancing while
        the file stays fresh is reading a wedged writer, not a live one.
      * ``writer`` — who wrote this beat (see the WRITER_* constants).
        Absent when the caller did not identify itself (legacy writers).
      * ``incarnation_id`` — ONLY when this process has bound one (see
        :func:`try_bind_incarnation`). Absent on observer beats and on
        beats written before the start path published the marker; absent
        is honest, a guessed id would be an echo.
    """
    out: dict = {}
    prev_seq = 0
    if isinstance(prev_beat, dict):
        try:
            prev_seq = int(prev_beat.get("seq", 0) or 0)
        except (TypeError, ValueError):
            prev_seq = 0
    out["seq"] = max(0, prev_seq) + 1
    if writer:
        out["writer"] = str(writer)
    incarnation = bound_incarnation(state_dir)
    if incarnation:
        out["incarnation_id"] = incarnation
    return out


# ---------------------------------------------------------------------------
# Exit reason holder (first cause wins)
# ---------------------------------------------------------------------------


class ExitReasonHolder:
    """First-cause-wins holder for the daemon's exit reason + code.

    Every path that decides the daemon must end records its cause here;
    ``set_once`` keeps the FIRST decision (the signal handler that
    initiated a shutdown must not be overwritten by the conversation
    task's subsequent — and expected — completion).
    """

    def __init__(self) -> None:
        self.reason: str | None = None
        self.code: int = 0

    def set_once(self, reason: str, code: int) -> bool:
        """Record ``(reason, code)`` if no cause is held yet.

        Returns True iff this call recorded the cause. An unknown
        ``reason`` raises — the vocabulary is closed on purpose.
        """
        if reason not in VALID_EXIT_REASONS:
            raise ValueError(
                f"unknown exit reason {reason!r}; valid: "
                f"{sorted(VALID_EXIT_REASONS)}"
            )
        if self.reason is not None:
            return False
        self.reason = reason
        self.code = int(code)
        return True


# ---------------------------------------------------------------------------
# ExitRecord
# ---------------------------------------------------------------------------


def write_exit_record(
    state_dir: Path,
    *,
    reason: str,
    code: int,
    incarnation_id: str | None = None,
    pid: int | None = None,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """Write the terminal ExitRecord to ``<state_dir>/exit.json``.

    Atomic (tmp + rename). ``reason`` must be in the closed vocabulary —
    a typo here would poison every downstream verdict, so it fails loud.
    Also mirrors the death onto the ``incarnations`` state.db row
    (best-effort — bookkeeping I/O must never mask the exit itself).
    Returns the record written.
    """
    if reason not in VALID_EXIT_REASONS:
        raise ValueError(
            f"unknown exit reason {reason!r}; valid: {sorted(VALID_EXIT_REASONS)}"
        )
    record = {
        "incarnation_id": incarnation_id,
        "reason": reason,
        "code": int(code),
        "ts": float(now_fn()),
        "pid": pid,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(Path(state_dir) / EXIT_RECORD_FILENAME, json.dumps(record))
    if incarnation_id:
        try:
            from .._state.state_db_incarnations import record_incarnation_exit

            record_incarnation_exit(
                incarnation_id, reason=reason, code=int(code)
            )
        except Exception as exc:  # stx-allow: fallback (reason: the state.db mirror is bookkeeping; exit.json is already written and a DB hiccup must not mask the real exit)
            logger.warning(
                "exit-record DB mirror failed for incarnation %s: %s",
                incarnation_id,
                exc,
            )
    return record


def read_exit_record(state_dir: Path) -> dict | None:
    """Return the ExitRecord dict, or None if absent / corrupt."""
    p = Path(state_dir) / EXIT_RECORD_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_exit_record(state_dir: Path) -> None:
    """Remove a previous incarnation's ExitRecord (called at daemon boot).

    ``exit.json`` present therefore always describes the MOST RECENT
    incarnation to have exited — a new boot must not leave its
    predecessor's farewell lying around to be read as its own.
    """
    p = Path(state_dir) / EXIT_RECORD_FILENAME
    try:
        p.unlink()
    except FileNotFoundError:
        pass
