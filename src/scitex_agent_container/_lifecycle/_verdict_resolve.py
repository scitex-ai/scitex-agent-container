"""Gather the real liveness signals and fold them into a :mod:`._verdict`.

This is ALL of the verdict's IO and nothing else — the pure decision rule
lives next door in :mod:`._verdict`, so the rule is testable without a
process, a socket or a tmux server, and this module is testable against real
ones. (Same split as ``_liveness_tick`` / ``_liveness_tick_detect`` /
``_liveness_tick_resolve``.)

Every resolver here obeys one contract: **a probe that could not run returns
UNKNOWN, never DEAD.** ``False`` and "I could not look" are different facts,
and only one of them may be acted on.

Timeouts are deliberately GENEROUS. The host this fleet runs on idles at load
60-70; a 2s probe against it is a coin toss, and a coin-toss timeout that
renders as DEAD is a random agent-killer. Every deadline below is sized to be
boring on a loaded box, because the cost of waiting an extra second is nothing
and the cost of a false DEAD is a destroyed agent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from ._verdict import (
    ALIVE,
    DEAD,
    SOURCE_DELIVERY,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    UNKNOWN,
    LivenessVerdict,
    Signal,
    decide,
)

__all__ = [
    "HEARTBEAT_STALE_S",
    "delivery_signal",
    "heartbeat_signal",
    "process_signal",
    "registry_signal",
    "resolve_verdict",
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


# --------------------------------------------------------------------------
# delivery — the ONLY authoritative signal. Never yields DEAD.
# --------------------------------------------------------------------------


def delivery_signal(
    name: str,
    *,
    probe: Callable[[str], tuple[int | None, str]] | None = None,
) -> Signal:
    """Did the broker OBSERVE a live inbox subscriber for ``name``?

    This is the one signal that asks the agent rather than inspecting its
    shadow: the ``sac listen`` broker knows whether the agent's inbox adapter
    is attached, which is the only fact that predicts whether a message will
    actually wake it.

    Deliberately CANNOT return :data:`DEAD`. Zero subscribers means a detached
    inbox adapter, not a corpse — :mod:`.._listen._reachability` says so in as
    many words ("``UNREACHABLE`` must NEVER be wired to anything destructive"),
    and an agent with a detached adapter is routinely alive and working. It
    degrades to :data:`UNKNOWN`, which authorises nothing.

    ``probe`` is the injection seam (a real callable returning the same
    ``(subscribers, reachable)`` tuple); production uses the real
    :func:`.inbox_probe.probe_inbox_reachability`.
    """
    from .._listen._reachability import REACHABLE, UNREACHABLE

    if probe is None:
        from .inbox_probe import probe_inbox_reachability as probe  # type: ignore

    try:
        subscribers, reachable = probe(name)
    except Exception as exc:  # stx-allow: fallback (an unaskable broker is UNKNOWN — never a death verdict against a healthy agent)
        return Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            f"could not ask the listen broker ({type(exc).__name__}) — "
            f"unobserved, NOT unreachable",
        )

    if reachable == REACHABLE:
        return Signal(
            SOURCE_DELIVERY,
            ALIVE,
            f"{subscribers} live inbox subscriber(s) — the broker can wake it",
        )
    if reachable == UNREACHABLE:
        # Observed ZERO subscribers on a bus we CAN see. That is evidence the
        # inbox adapter is detached. It is NOT evidence the agent is dead, and
        # rendering it as such would slander (and get us to kill) a healthy,
        # working agent — measured: agents with 0 subscribers and an unbound
        # /v1/turn have answered peer messages the same minute.
        return Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            "0 inbox subscribers — the inbox adapter is DETACHED, which is "
            "not death; the agent may still be alive and working",
        )
    return Signal(
        SOURCE_DELIVERY,
        UNKNOWN,
        "the broker cannot observe this agent (no local listen, or it lives "
        "on another host) — unobserved, NOT unreachable",
    )


# --------------------------------------------------------------------------
# process — a session/pid probe. Ternary: the probe can FAIL.
# --------------------------------------------------------------------------


def _in_sif() -> bool:
    """Are we running INSIDE an apptainer SIF (so the host's tmux is invisible)?

    Reuses the canonical predicate the spawn/status brokers already key off
    (:func:`.._lifecycle._in_sif_broker.is_in_sif`) rather than sniffing for
    apptainer markers a second time — one definition of "am I in a container",
    so the probe and the brokers can never disagree.
    """
    try:
        from ._in_sif_broker import is_in_sif

        return bool(is_in_sif())
    except Exception:  # stx-allow: fallback (if we cannot even tell where we are, assume the cautious answer: we might be blind)
        return True


def _tmux_probe_ran(
    socket_name: str | None = None,
    *,
    snapshot_fn: Callable[..., dict | None] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> bool | None:
    """Did a tmux probe run that could actually SEE this fleet's sessions?

    ``True`` = yes, so a "no session" answer is a real observation of absence.
    ``None`` = no, so a "no session" answer means "I could not look".

    Two distinct ways the probe fails to see the fleet, and BOTH must map to
    ``None`` — one of them bit this very module during development:

    1. **The probe errored / tmux is wedged.**
       :func:`.._runners._tmux._tmux_probe.list_sessions_activity` returns
       ``None`` for this, exactly as its contract says.

    2. **We are inside a container, and the host's tmux is in another mount
       namespace.** This one is a TRAP, because the probe does not error — it
       SUCCEEDS and reports an EMPTY fleet. From inside a SIF, ``tmux ls``
       prints ``no server running on /tmp/tmux-1000/default`` (true! for the
       CONTAINER's own /tmp), which is one of ``_tmux_probe``'s
       "no server ⇒ confirmed-empty" markers, so ``list_sessions_activity()``
       returns ``{}`` — "the probe succeeded and the fleet is genuinely empty".

       MEASURED (2026-07-14): from inside this container that path made
       ``process_signal`` return DEAD for ``grant`` — an agent holding a live
       tmux session, a fresh heartbeat and a live inbox subscriber on the host.
       A confident, well-evidenced, completely false death verdict. Only the
       corroboration gate stopped it authorising anything.

       So an empty snapshot taken from inside a SIF is NOT evidence of absence.
       It is the same fact ``_listen._reachability`` already encodes for
       cross-host peers: *a thing you are not in a position to observe must be
       UNKNOWN, and never accused.*

    ``snapshot_fn`` / ``in_sif_fn`` are injection seams taking REAL callables
    (production resolves the real probe + the real in-SIF predicate).
    """
    snapshot_fn = snapshot_fn or _real_tmux_snapshot
    in_sif_fn = in_sif_fn or _in_sif

    try:
        snapshot = snapshot_fn(socket_name=socket_name)
    except Exception:  # stx-allow: fallback (cannot even ask tmux → we do not know whether a probe would have run)
        return None

    if snapshot is None:
        return None  # the probe FAILED — its own contract already says so.
    if not snapshot and in_sif_fn():
        # An "empty fleet" seen from inside a container is namespace blindness,
        # not an empty fleet. Refuse to treat a non-observation as one.
        return None
    return True


def _real_tmux_snapshot(*, socket_name: str | None = None) -> dict | None:
    """The real batched tmux probe. Kept behind a seam so tests drive the RULE
    above without needing a live tmux server in a specific namespace."""
    from .._runners._tmux._tmux_probe import list_sessions_activity

    return list_sessions_activity(socket_name=socket_name)


def process_signal(
    config: Any,
    runtime: Any,
    *,
    tmux_probe_ran: Callable[[], bool | None] | None = None,
) -> Signal:
    """Is a process/session for this agent observably up?

    ``runtime.is_running`` is a BOOL, and its ``False`` conflates two very
    different facts: "I probed and there is nothing there" and "I could not
    probe". This wraps it back into a ternary:

    * raises                       → :data:`UNKNOWN` (the probe blew up)
    * ``True``                     → :data:`ALIVE`
    * ``False`` + probe DID run    → :data:`DEAD` (positive: nothing is there)
    * ``False`` + probe did NOT run→ :data:`UNKNOWN` (a wedged/invisible tmux)

    The last case is the one that matters. ``TuiSessionRuntime.is_running``
    bottoms out in ``TmuxManager.exists``, which returns ``False`` both for
    "no such session" and for "I cannot talk to tmux at all" — and TUI is this
    fleet's DEFAULT runtime, so collapsing those two would mark every agent
    dead the moment the tmux server hiccups.
    """
    runtime_kind = str(getattr(config, "runtime", "") or "")
    is_tui = runtime_kind == "tui"

    try:
        running = runtime.is_running(config)
    except Exception as exc:  # stx-allow: fallback (a probe that raised observed NOTHING — UNKNOWN, never DEAD)
        return Signal(
            SOURCE_PROCESS,
            UNKNOWN,
            f"liveness probe raised {type(exc).__name__}: {exc} — the probe "
            f"did not run, which is not evidence of death",
        )

    if running:
        return Signal(
            SOURCE_PROCESS,
            ALIVE,
            f"{runtime_kind or 'runtime'} probe: process/session is up",
        )

    if is_tui:
        ran_fn = tmux_probe_ran or _tmux_probe_ran
        ran = ran_fn()
        if ran is not True:
            return Signal(
                SOURCE_PROCESS,
                UNKNOWN,
                "the tmux probe itself FAILED (wedged tmux, or this process "
                "cannot see the tmux socket) — 'no session' here means 'I "
                "could not look', not 'the agent is gone'",
            )
        return Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe SUCCEEDED and this agent has no live session/pane "
            "process — positive evidence of absence",
        )

    return Signal(
        SOURCE_PROCESS,
        DEAD,
        f"{runtime_kind or 'runtime'} probe succeeded and reports no running "
        f"process — positive evidence of absence",
    )


# --------------------------------------------------------------------------
# heartbeat — the agent's OWN writer. Fresh beat ⇒ alive.
# --------------------------------------------------------------------------


def heartbeat_signal(
    name: str,
    *,
    now: float | None = None,
    stale_s: float = HEARTBEAT_STALE_S,
    path: Path | None = None,
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

    NEVER RETURNS :data:`DEAD`, on purpose. A stale beat has two
    indistinguishable causes — the agent went away, or the shared writer inside
    ``sac listen`` stopped (a loop known to blow its budget and get abandoned,
    which freezes EVERY agent's beat at once). Worse, for a TUI agent this
    signal is DERIVED FROM THE SAME tmux probe as :func:`process_signal`, so
    convicting on it would not be independent corroboration — it would be the
    same observation, counted twice. :func:`process_signal` already carries the
    DEAD case, honestly and once.
    """
    now = time.time() if now is None else now
    hb = path if path is not None else _heartbeat_path(name)

    try:
        mtime = hb.stat().st_mtime
    except OSError:  # stx-allow: fallback (no heartbeat file at all → no channel that could have shown life → UNKNOWN, never DEAD)
        return Signal(
            SOURCE_HEARTBEAT,
            UNKNOWN,
            f"no heartbeat file at {hb} — no channel that could have shown "
            f"life, so nothing is proven either way",
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
        )

    return Signal(
        SOURCE_HEARTBEAT,
        UNKNOWN,
        f"beat is {age:.0f}s stale — but the writer is a shared loop inside "
        f"sac listen, so this looks identical whether the agent went away or "
        f"the writer did. It convicts nobody{pid_note}",
    )


# --------------------------------------------------------------------------
# registry — a DECLARATION. Asymmetric: a reaped pid is proof, a live one is not.
# --------------------------------------------------------------------------


def registry_signal(
    name: str,
    *,
    rows: list[dict] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> Signal:
    """What does the ``instances`` table DECLARE, and can we corroborate it?

    A registry row is a declaration someone wrote once, not an observation. It
    can vouch for a corpse, and on this fleet it is routinely missing (or
    carries ``pid = NULL``) for perfectly healthy agents. So:

    * no row / unreadable table / ``pid`` NULL → :data:`UNKNOWN`. Absence of a
      row is NOT evidence of death; that inference is what alarmed ~100 false
      criticals per sweep against agents serving HTTP in the same log.
    * recorded pid CONFIRMED REAPED → :data:`DEAD`. This is genuinely positive:
      ``os.kill(pid, 0)`` raising ESRCH means *that* process does not exist.
    * recorded pid alive → :data:`UNKNOWN`, deliberately NOT ALIVE. Pids are
      RECYCLED, so a live pid may belong to an unrelated process that has
      merely inherited the number, and it would then vouch for a dead agent.
      The evidence is asymmetric and we grade it that way.
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
            )

    mine = [r for r in (rows or ()) if r.get("name") == name]
    if not mine:
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            "no active instances row — a declaration is missing, which is not "
            "evidence of death (healthy agents routinely have no row here)",
        )

    pid = mine[0].get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            f"active row declares this agent running, but records pid={pid!r} "
            f"— nothing to corroborate against, so no verdict",
        )

    if pid_alive(pid):
        return Signal(
            SOURCE_REGISTRY,
            UNKNOWN,
            f"active row, recorded pid={pid} is alive — corroborating, but "
            f"pids are RECYCLED so this alone does not prove it is OUR process",
        )
    return Signal(
        SOURCE_REGISTRY,
        DEAD,
        f"active row records pid={pid}, and that pid is REAPED — the process "
        f"the registry points at does not exist",
    )


# --------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------


def resolve_verdict(
    name: str,
    config: Any | None = None,
    runtime: Any | None = None,
    *,
    delivery: Callable[[str], Signal] | None = None,
    process: Callable[[Any, Any], Signal] | None = None,
    heartbeat: Callable[[str], Signal] | None = None,
    registry: Callable[[str], Signal] | None = None,
) -> LivenessVerdict:
    """Gather every signal we can, then fold them with :func:`._verdict.decide`.

    Signals we cannot gather are simply absent — an absent signal contributes
    nothing, which is right: it neither convicts nor exonerates. A verdict with
    no signals at all is :data:`UNKNOWN`, and that is the honest answer.

    Every collaborator is an injection seam taking REAL callables (no mocks —
    the suite drives real tmux sockets, real processes, real files through
    these).
    """
    signals: list[Signal] = []

    signals.append((delivery or delivery_signal)(name))

    if config is not None and runtime is not None:
        signals.append((process or process_signal)(config, runtime))

    signals.append((heartbeat or heartbeat_signal)(name))
    signals.append((registry or registry_signal)(name))

    return decide(name, signals)
