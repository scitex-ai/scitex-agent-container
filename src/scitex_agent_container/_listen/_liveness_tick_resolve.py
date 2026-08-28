"""Liveness SIGNAL RESOLVERS for the liveness-tick reconciler (blocking IO).

Card ``sac-card-anchored-stop-reconciler``. Split out of ``_liveness_tick``
(the loop/emit glue) alongside ``_liveness_tick_detect`` (the pure rule), so
the three concerns are separately testable: this module is ALL of the
reconciler's blocking IO and nothing else.

Everything here is called from inside ``run_blocking_or`` — never on the
event loop — and every reader is fail-soft: a signal we cannot read yields
"no record", which the rule reads as UNKNOWN, never as death.

WHY THREE SIGNALS
-----------------
The reconciler used to gate liveness on ONE signal, the instances registry:
no live registry pid ⇒ ``owner-not-live``. On the live fleet EVERY active
registry row carries ``pid = NULL``, so no owner could ever resolve live and
every stale card alarmed ``critical`` against agents that were provably
alive. The registry is corroborating evidence, not a gate, and the agents'
OWN records outrank it:

* ``session.jsonl`` last-record ts — PROGRESS (SDK runtimes).
* ``heartbeat.json`` ``ts`` field  — PROGRESS (the TUI runtimes that make up
  the fleet write no session.jsonl at all, so without this there is no
  progress signal for them whatsoever).
* ``heartbeat.json`` MTIME         — PROCESS ALIVE. The heartbeat writer runs
  inside the live agent, so a fresh beat proves the process exists even when
  the registry has no pid for it.
"""

from __future__ import annotations

import logging
import os
from datetime import timezone
from pathlib import Path
from typing import Iterable

from ._liveness_tick_detect import AgentLiveness, open_card_owners

logger = logging.getLogger(__name__)

# Tail window for the session.jsonl read. The last record lives at the end of
# the file, so an O(1) tail beats an O(file) scan — and this now runs for
# EVERY open-card owner on EVERY tick.
_SESSION_TAIL_BYTES = 1 << 20  # 1 MiB

_HEARTBEAT_FILENAME = "heartbeat.json"


def _default_tasks_path() -> Path:
    """Resolve the scitex-todo card-store path this reconciler reads.

    This is the load-bearing scitex-todo handoff (the liveness-tick reads
    OPEN cards as truth); scitex-todo owns this path, so the legacy
    ``SCITEX_TODO_TASKS_YAML_SHARED`` env var and the on-disk default live
    HERE (not in ``_ci_owner``, whose CI-owner lookup is now sac-local).
    Removed once scitex-todo takes over the stuck-card reconciliation."""
    env = os.environ.get("SCITEX_TODO_TASKS_YAML_SHARED")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".scitex" / "todo" / "tasks.yaml"


def _load_tasks_doc(tasks_path: Path) -> dict:
    """Read + parse ``tasks.yaml``. Fail-soft: unreadable ⇒ ``{}``."""
    if not tasks_path.is_file():
        return {}
    import yaml

    try:
        doc = yaml.safe_load(tasks_path.read_text()) or {}
    except Exception:  # stx-allow: fallback (unreadable tasks store → no cards this tick)
        return {}
    return doc if isinstance(doc, dict) else {}


def _line_ts(line: str) -> float | None:
    """Epoch seconds of one JSONL record's ``ts``/``timestamp``, else ``None``.

    Delegates to the same ``_record_ts`` parser the SSE tail uses, so "what
    counts as a record timestamp" has exactly one definition in this package."""
    from ._tail import _record_ts

    line = line.strip()
    if not line:
        return None
    import json as _json

    try:
        record = _json.loads(line)
    except ValueError:  # stx-allow: fallback (a torn/partial line is not a record)
        return None
    if not isinstance(record, dict):
        return None
    dt = _record_ts(record)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _full_scan_last_ts(path: Path) -> float | None:
    """Forward-stream the WHOLE file, keeping the last parseable record ts.

    O(file), so this is the RARE fallback only (see
    :func:`_session_last_active_ts`). Streams line-by-line — never
    ``readlines()`` — so an enormous session cannot blow up the daemon's RSS."""
    last_ts: float | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                ts = _line_ts(line)
                if ts is not None:
                    last_ts = ts
    except OSError:  # stx-allow: fallback (unreadable session → unknown activity)
        return None
    return last_ts


def _session_last_active_ts(name: str) -> float | None:
    """Epoch seconds of ``name``'s session.jsonl last-record, or ``None``.

    A POSITIVE PROOF OF PROGRESS: an agent appending session records right
    now is alive and working. :func:`resolve_liveness` reads this
    UNCONDITIONALLY. It used to be read only once the registry had ALREADY
    said "live", so the strongest positive signal was never consulted for
    exactly the agents the registry missed — which is the entire population
    that flooded the log.

    Reads only the last ``_SESSION_TAIL_BYTES`` and scans BACKWARDS for the
    newest record carrying a ``ts``/``timestamp``, so the cost is O(1) in the
    session size rather than O(file). That matters now that this runs for
    EVERY open-card owner on EVERY tick: session.jsonl grows without bound,
    and a full re-scan per owner per tick would burn CPU on the very daemon
    this reconciler exists to keep responsive. The full scan survives only as
    the fallback for a session whose final record is larger than the window.

    Fail-soft: a missing / unreadable / ts-less session yields ``None`` — no
    activity record, which is UNKNOWN activity and NOT evidence of death."""
    from ._tail import _runtime_session_jsonl

    path = _runtime_session_jsonl(name)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _SESSION_TAIL_BYTES)
            fh.seek(start)
            blob = fh.read()
    except OSError:  # stx-allow: fallback (unreadable session → unknown activity)
        return None

    lines = blob.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the window cut this line mid-record — drop it
    for line in reversed(lines):
        ts = _line_ts(line)
        if ts is not None:
            return ts
    if start == 0:
        return None  # the whole file was scanned: there is genuinely no ts
    # The final record is larger than the tail window. Pay for the full scan
    # rather than mis-report an ACTIVE agent as having no activity record.
    return _full_scan_last_ts(path)


def _heartbeat_signals(name: str) -> tuple[float | None, float | None]:
    """``(beat_ts, activity_ts)`` read from ``name``'s ``heartbeat.json``.

    The heartbeat is the ONE record every runtime writes — the TUI agents
    that make up the fleet write no session.jsonl at all — so without it the
    rule has no positive signal to weigh the registry against.

    * ``beat_ts`` — the file's MTIME: when the agent's heartbeat writer last
      beat. The writer runs inside the live agent, so a fresh beat is PROOF
      THE PROCESS IS ALIVE. It deliberately does NOT prove progress: a wedged
      agent keeps beating, and that is precisely the ``owner-idle`` case.
    * ``activity_ts`` — the record's ``ts`` field: the agent's last real
      activity (the TUI heartbeat writer stores the tmux pane-activity epoch
      here). A PROGRESS signal, folded into ``last_active_ts``.

    Fail-soft: missing / unreadable / unparseable ⇒ ``(None, None)`` — no
    record at all, which is UNKNOWN and never "dead"."""
    from ._tail import _runtime_session_jsonl

    path = _runtime_session_jsonl(name).parent / _HEARTBEAT_FILENAME
    try:
        beat_ts = path.stat().st_mtime
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # stx-allow: fallback (no/unreadable heartbeat → no record)
        return None, None
    import json as _json

    try:
        record = _json.loads(raw)
    except ValueError:  # stx-allow: fallback (torn heartbeat write → beat only)
        return beat_ts, None
    if not isinstance(record, dict):
        return beat_ts, None
    ts = record.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return beat_ts, float(ts)
    return beat_ts, None


def _live_agent_pids(db_path: Path | None = None) -> dict[str, int] | None:
    """Map active-agent name → recorded pid — or ``None`` if the registry
    could not be READ AT ALL.

    Reuses the existing ``list_active_instances`` reader (``ended_at IS NULL``
    rows) rather than reinventing a liveness store.

    The ``None`` return is load-bearing and must NOT be collapsed back into
    ``{}``:

    * ``{}``   — the registry WAS read, and lists nobody with a live pid.
    * ``None`` — the registry could not be read. We know NOTHING.

    The previous version returned ``{}`` for both and called it "the SAFE
    direction". It is the exact opposite: a registry that cannot be read makes
    EVERY owner resolve not-live, so EVERY stale card alarms
    ``owner-not-live`` — absence of evidence rendered as evidence of death.

    ``{}`` is ALSO not proof of death, for the same reason: this fleet's
    registry records ``pid = NULL`` on every active row, so a perfectly
    healthy agent contributes no entry here. Callers must corroborate a
    missing pid against the owner's heartbeat before concluding anything —
    :func:`resolve_liveness` does."""
    try:
        from .._state.state_db import list_active_instances

        rows = list_active_instances()
    except Exception as exc:  # stx-allow: fallback (registry unreadable → UNKNOWN, never "dead")
        logger.warning(
            "liveness_tick: instances registry unavailable (%s) — owner "
            "liveness is UNKNOWN this tick; owner-not-live detection is "
            "SUSPENDED (a registry failure must never read as agent death)",
            exc,
        )
        return None
    out: dict[str, int] = {}
    for row in rows or []:
        try:
            name = str(row.get("name", "")).strip()
            pid = row.get("pid")
            if name and isinstance(pid, int) and not isinstance(pid, bool):
                out.setdefault(name, pid)  # newest row wins (DESC order)
        except Exception:  # stx-allow: fallback (one bad row contributes nothing)
            continue
    return out


def resolve_liveness(
    owners: Iterable[str],
    *,
    db_path: Path | None = None,
) -> dict[str, AgentLiveness]:
    """Resolve each owner agent → :class:`AgentLiveness` (BLOCKING — run
    off-loop).

    Reads three independent signals per owner and — the whole point — reads
    the POSITIVE ones UNCONDITIONALLY, never gated on the registry's verdict:

    * ``last_active_ts`` — newest ACTIVITY record: the owner's session.jsonl
      last record, or its heartbeat's ``ts``, whichever is newer. PROGRESS.
    * ``last_beat_ts``   — the owner's heartbeat MTIME. PROCESS ALIVE.
    * ``is_live``        — the registry's verdict (active row + live pid).
      Corroborating evidence only, no longer a gate: every row on the live
      fleet carries ``pid = NULL``, so this reads ``False`` for healthy agents
      exactly as often as for dead ones.

    ``known`` is the honest "could we have seen life, had there been any?"
    flag: true iff the registry was readable AND the owner has at least one
    channel a live agent would have refreshed (a recorded pid, or a heartbeat
    file). When it is false the owner is UNKNOWN and the rule stays silent —
    it never guesses "dead".

    ``db_path`` overrides the registry location (tests point it at a real temp
    SQLite file; production leaves it ``None``)."""
    from ._agent_exec_liveness import _pid_alive

    pids = _live_agent_pids(db_path)
    registry_ok = pids is not None
    pids = pids or {}

    out: dict[str, AgentLiveness] = {}
    for owner in owners:
        beat_ts, beat_activity_ts = _heartbeat_signals(owner)
        session_ts = _session_last_active_ts(owner)
        seen = [t for t in (session_ts, beat_activity_ts) if t is not None]
        last_active = max(seen) if seen else None

        pid = pids.get(owner)
        is_live = bool(pid is not None and _pid_alive(pid))
        known = registry_ok and (pid is not None or beat_ts is not None)

        out[owner] = AgentLiveness(
            is_live=is_live,
            last_active_ts=last_active,
            known=known,
            last_beat_ts=beat_ts,
        )
    return out


def fleet_last_beat_ts() -> float | None:
    """Newest heartbeat MTIME across the WHOLE fleet, or ``None`` if there is
    no heartbeat anywhere.

    This answers ONE question the per-owner signals cannot: **is the heartbeat
    writer itself still working?** The writer is a single shared loop inside
    ``sac listen`` that is known to blow its budget and get abandoned; when it
    stops, every agent's beat freezes at once. The rule needs to tell that
    apart from "the agents died", and it can: if ANY agent in the fleet is
    still being beaten for, the writer works, and a frozen beat then really
    does convict the agent it belongs to.

    Deliberately scans the whole runtime root and NOT just the card owners:
    owners of stale cards are a biased sample (skewed toward dead agents), so
    inferring writer health from their silence would let a lone dead owner
    suppress its own alarm.

    Cost is one ``scandir`` + one ``stat`` per agent dir — no file is opened —
    so it stays O(agents) and cheap enough for every tick."""
    root = Path(os.path.expanduser("~")) / ".scitex" / "agent-container" / "runtime"
    newest: float | None = None
    try:
        entries = list(os.scandir(root))
    except OSError:  # stx-allow: fallback (no runtime root → no fleet reading)
        return None
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            mtime = (Path(entry.path) / _HEARTBEAT_FILENAME).stat().st_mtime
        except OSError:  # stx-allow: fallback (no heartbeat for this agent)
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _resolve_doc_and_liveness(
    tasks_path: Path,
) -> tuple[dict, dict[str, AgentLiveness], float | None]:
    """One blocking unit: load the doc, resolve liveness for exactly the owners
    of its OPEN, unblocked cards, and take one fleet-wide reading of the
    heartbeat writer's health. Bundled so the loop makes a SINGLE off-loop call
    per tick."""
    doc = _load_tasks_doc(tasks_path)
    owners = open_card_owners(doc)
    liveness = resolve_liveness(owners) if owners else {}
    return doc, liveness, fleet_last_beat_ts()


__all__ = [
    "fleet_last_beat_ts",
    "resolve_liveness",
]
