"""Liveness signals read from a STATE ARTEFACT that somebody else wrote.

Split out of :mod:`._verdict_resolve` (512-line cap) along a real seam.
:mod:`._verdict_resolve` PROBES — it asks the broker, it asks the runtime. This
module READS THINGS OTHER PROCESSES LEFT BEHIND: a heartbeat file and a row in
the ``instances`` table.

That difference is the whole reason both resolvers here are so cautious. An
artefact is a claim made by a writer who is not the agent and is not us:

* it can be STALE because its WRITER died, not because the agent did (the TUI
  heartbeat is written by a shared loop inside ``sac listen``, so when that loop
  is abandoned EVERY agent's beat freezes at once — a fact about the writer,
  reported as if it were a fact about twenty agents);
* it can OUTLIVE the thing it describes (a registry row survives the process, so
  a stopped agent's row still declares it running);
* and the only thing in here that actually OBSERVES anything is
  ``os.kill(pid, 0)`` — which is not a sensor at all unless we are in the pid
  namespace that minted the pid.

So: :func:`heartbeat_signal` NEVER convicts, and :func:`registry_signal` convicts
only on a pid it was genuinely in a position to read.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from ._verdict import (
    ALIVE,
    DEAD,
    INSTRUMENT_AGENT_SELF,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_PID_NAMESPACE,
    SOURCE_HEARTBEAT,
    SOURCE_REGISTRY,
    UNKNOWN,
    Signal,
)
from ._verdict_tmux import pid_namespace_is_observable

__all__ = [
    "HEARTBEAT_STALE_S",
    "heartbeat_signal",
    "registry_signal",
]

# A heartbeat older than this is no longer evidence of life. Ten minutes:
# the writers beat about every 30s, so this is ~20 missed beats — long enough
# that a loaded box, a slow FS or a stop-the-world GC cannot manufacture it,
# short enough to be useful. It is NOT, on its own, evidence of DEATH; see
# :func:`heartbeat_signal`.
HEARTBEAT_STALE_S = 600.0

_HEARTBEAT_FILENAME = "heartbeat.json"


def _runtime_root() -> Path:
    """The per-agent runtime state root (``~/.scitex/agent-container/runtime``).

    Resolved at CALL time, never captured into a module-level constant. A
    ``Path.home()``-derived constant computed at IMPORT cannot be redirected by
    a fixture that sets ``$HOME`` — that exact bug had a suite in this repo
    reading and WRITING the real fleet registry, and it is invisible in CI (no
    fleet there, so it passes).

    KNOWN BLIND SPOT, and it is deliberately left blind rather than guessed at:
    inside a container ``$HOME`` is ``/home/agent``, not the operator's home, so
    this resolves to a directory that does not exist and the heartbeat signal
    comes back UNKNOWN. That is the CORRECT degradation — "I cannot see this
    agent's heartbeat from here" — and UNKNOWN authorises nothing. The tempting
    fix (guess at the operator's home and read a path we were not given) trades
    an honest UNKNOWN for a confident answer about a file we are not sure is the
    right one, which is how this class of bug is born in the first place.
    """
    return Path(os.path.expanduser("~")) / ".scitex" / "agent-container" / "runtime"


def _heartbeat_path(name: str) -> Path:
    return _runtime_root() / name / _HEARTBEAT_FILENAME


def heartbeat_signal(
    name: str,
    *,
    now: float | None = None,
    stale_s: float = HEARTBEAT_STALE_S,
    path: Path | None = None,
    runtime_kind: str = "",
) -> Signal:
    """Has this agent's heartbeat been refreshed recently?

    WHO WRITES IT (this is not what the file's name suggests, and the
    difference is the whole reason ``pid: 0`` misled everyone):
    ``heartbeat.json`` for a TUI agent is written by a CENTRALIZED loop inside
    ``sac listen`` (:func:`._tui_heartbeat_loop.tui_heartbeat_loop`), not by
    the agent. That loop takes ONE batched ``tmux list-sessions`` snapshot and
    beats only for agents whose ``tui-<name>`` session is actually IN it — and
    a FAILED probe returns ``None`` and skips the whole tick, so a false beat
    is never written. So:

    * a FRESH mtime is sound evidence of life: ``sac listen`` observed this
      agent's session in a probe that SUCCEEDED, seconds ago.
    * the record's ``pid`` field is a **hardcoded literal 0**
      (``write_fn(state_dir, pid=0, ...)``) — the central writer does not know
      the pane pid and never did. It is not a signal, it never was one, and
      nothing may decide anything on it. We surface it only so the next person
      to read ``pid: 0`` is told, in words, that it means nothing.

    WHICH INSTRUMENT THIS IS depends on WHO WROTE THE FILE, which is why
    ``runtime_kind`` is threaded in:

    * ``tui`` → :data:`INSTRUMENT_HOST_TMUX`. The beat is a RE-REPORT of the very
      same ``tmux list-sessions`` snapshot :func:`._verdict_resolve.process_signal`
      reads. It is not a second sensor, and labelling it as one would let ``sac
      listen``'s belief vote twice.
    * anything else → :data:`INSTRUMENT_AGENT_SELF`: the SDK loop runs INSIDE the
      agent, so a fresh beat really is the agent saying "I am here".

    NEVER RETURNS :data:`DEAD`, on purpose — and that is now ENFORCED by both
    instruments' specs rather than merely intended. A stale beat has two
    indistinguishable causes: the agent went away, or the shared writer inside
    ``sac listen`` stopped (a loop known to blow its budget and get abandoned,
    which freezes EVERY agent's beat at once).
    """
    now = time.time() if now is None else now
    hb = path if path is not None else _heartbeat_path(name)
    instrument = (
        INSTRUMENT_HOST_TMUX if runtime_kind == "tui" else INSTRUMENT_AGENT_SELF
    )

    try:
        mtime = hb.stat().st_mtime
    except OSError:  # stx-allow: fallback (no heartbeat file at all → no channel that could have shown life → UNKNOWN, never DEAD)
        return Signal(
            SOURCE_HEARTBEAT,
            UNKNOWN,
            f"no heartbeat file at {hb} — no channel that could have shown "
            f"life, so nothing is proven either way",
            instrument,
        )

    age = now - mtime
    # Surface the record's pid purely as a DE-MYSTIFIER. See the docstring:
    # for a TUI agent it is a hardcoded 0 written by sac listen.
    pid_note = ""
    try:
        import json as _json

        record = _json.loads(hb.read_text(encoding="utf-8", errors="replace"))
        if isinstance(record, dict):
            pid = record.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid <= 0:
                pid_note = (
                    f"; record pid={pid} is a HARDCODED literal from the "
                    f"central listen-side writer, not a fact about the agent "
                    f"— it decides nothing"
                )
            elif isinstance(pid, int) and not isinstance(pid, bool):
                pid_note = f"; record pid={pid}"
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (a torn heartbeat write still has a usable mtime; the pid note is a nicety)
        pass

    if age < stale_s:
        return Signal(
            SOURCE_HEARTBEAT,
            ALIVE,
            f"beaten {age:.0f}s ago (< {stale_s:.0f}s) — sac listen observed "
            f"this agent's session in a SUCCESSFUL probe that recently{pid_note}",
            instrument,
        )

    return Signal(
        SOURCE_HEARTBEAT,
        UNKNOWN,
        f"beat is {age:.0f}s stale — but the writer is a shared loop inside "
        f"sac listen, so this looks identical whether the agent went away or "
        f"the writer did. It convicts nobody{pid_note}",
        instrument,
    )


def registry_signal(
    name: str,
    *,
    rows: list[dict] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> Signal:
    """What does the ``instances`` table DECLARE, and can we corroborate it?

    A registry row is a declaration someone wrote once, not an observation. It
    can vouch for a corpse, and on this fleet it is routinely missing (or
    carries ``pid = NULL``) for perfectly healthy agents. The only thing here
    that ever OBSERVES anything is the pid check, so:

    * no row / unreadable table / ``pid`` NULL → :data:`UNKNOWN`, on
      :data:`INSTRUMENT_NO_OBSERVATION` — the sensor never ran. Absence of a row
      is NOT evidence of death; that inference is what alarmed ~100 false
      criticals per sweep against agents serving HTTP in the same log.
    * we cannot READ this pid namespace (we are in a container, or the row was
      written on another host) → :data:`UNKNOWN`. A pid across a namespace
      boundary is not a weak sensor; it is not a sensor.
    * recorded pid CONFIRMED REAPED → :data:`DEAD`. Genuinely positive:
      ``os.kill(pid, 0)`` raising ESRCH means *that* process does not exist.
    * recorded pid alive → :data:`UNKNOWN`, deliberately NOT ALIVE. Pids are
      RECYCLED, so a live pid may belong to an unrelated process that has
      merely inherited the number, and it would then vouch for a dead agent.

    THE INSTRUMENT: every verdict this function can actually OBSERVE comes out of
    ``os.kill(pid, 0)`` — :data:`INSTRUMENT_PID_NAMESPACE`. That is the same
    instrument :func:`._verdict_resolve.process_signal` uses for a pid-based
    runtime, and the same one it falls back to for a TUI agent whose session
    exists but whose pane pid is gone. Both runtimes deliberately record the pid
    that ``is_running`` probes ("the registry and ``is_running`` can never
    disagree"), so the "two" witnesses are one. The gate now knows that.
    """
    if pid_alive is None:
        from .._listen._agent_exec_liveness import (
            _pid_alive as pid_alive,  # type: ignore
        )

    if rows is None:
        try:
            from .._state.state_db import list_active_instances

            rows = list_active_instances(host=None)
        except Exception as exc:  # stx-allow: fallback (an unreadable registry is UNKNOWN — reading it as "every agent is dead" is the documented flood)
            return Signal(
                SOURCE_REGISTRY,
                UNKNOWN,
                f"instances table unreadable ({type(exc).__name__}) — a "
                f"registry failure is not agent death",
                INSTRUMENT_NO_OBSERVATION,
            )

    mine = [r for r in (rows or ()) if r.get("name") == name]
    if not mine:
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            "no active instances row — a declaration is missing, which is not "
            "evidence of death (healthy agents routinely have no row here)",
            INSTRUMENT_NO_OBSERVATION,
        )

    row = mine[0]
    pid = row.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            f"active row declares this agent running, but records pid={pid!r} "
            f"— nothing to corroborate against, so no verdict",
            INSTRUMENT_NO_OBSERVATION,
        )

    observable, why_not = pid_namespace_is_observable(
        row_host=row.get("host"), in_sif_fn=in_sif_fn
    )
    if not observable:
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            f"active row records pid={pid}, but {why_not}",
            INSTRUMENT_PID_NAMESPACE,
        )

    if pid_alive(pid):
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            f"active row, recorded pid={pid} is alive — corroborating, but "
            f"pids are RECYCLED so this alone does not prove it is OUR process",
            INSTRUMENT_PID_NAMESPACE,
        )
    return Signal(
        SOURCE_REGISTRY,
        DEAD,
        f"active row records pid={pid}, and that pid is REAPED — the process "
        f"the registry points at does not exist",
        INSTRUMENT_PID_NAMESPACE,
    )
