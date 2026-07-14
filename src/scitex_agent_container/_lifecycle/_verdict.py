"""ALIVE / DEAD / UNKNOWN — the ternary liveness verdict, with its evidence.

Liveness in this codebase was a BOOL, and "I could not tell" was silently
collapsed into one of the two poles — then ACTED on. Both directions have
cost us:

* **false GREEN** — ``runtime.is_running`` is PID/pane-shaped, so a wedged or
  auth-dead agent whose process still exists reads ``running`` forever.
  ``scitex-clew`` sat dead for two days behind a green row.
* **false RED** — a probe that could not run returned ``False``, and ``False``
  is indistinguishable from "confirmed absent". The remedy a caller reaches
  for on a death verdict (``--force --fresh``, ``systemctl restart``) DESTROYS
  the thing it misdiagnosed. That is the strictly worse direction, and it is
  why this module refuses to manufacture a ``DEAD``.

The shape here is not new. It is the SAME ternary the a2a inbox already uses
(:mod:`.._listen._reachability`: ``reachable`` / ``unreachable`` / ``unknown``,
where an unobservable peer is never accused), and the same one
:func:`.._runners._tmux._tmux_probe.list_sessions_activity` states in its
return contract (``dict`` = probed, ``{}`` = confirmed empty, ``None`` =
probe FAILED, liveness UNKNOWN — *"callers must NOT read this as 'every agent
is dead'"*). This module generalises that discipline to liveness at large
rather than inventing a competing abstraction.

Three rules, and they are the whole module
------------------------------------------
1. **A verdict is never a bool.** ``pid <= 0``, an absent heartbeat, a probe
   that timed out, a peer on a host we cannot see → :data:`UNKNOWN`, named,
   with the reason attached. Not ``False``.

2. **Positive evidence of life is never overruled.** If ANY signal observed
   the agent alive, the verdict is :data:`ALIVE` — whatever the other signals
   say. This is not a heuristic, it is a lesson: an earlier
   ``pid AND port AND session_id`` predicate was refuted by ``scitex-writer``,
   which carried a stale ``startup_failed`` status, an unbound port and a null
   session_id — and was answering messages. Every one of those "dead" signals
   was a PROXY. The one that observed the agent itself said alive. Proxies
   lose.

3. **Only a CORROBORATED DEAD may authorise destruction.** See
   :attr:`LivenessVerdict.may_destroy`. :data:`UNKNOWN` authorises NOTHING
   destructive — it may report, and it may permit a NON-destructive recovery
   (starting a process is not destroying one), but it may never tear anything
   down. When in doubt we report and stop.

What each signal is worth
-------------------------
``delivery``
    The ONLY authoritative signal. The broker inside ``sac listen`` knows
    whether an agent's inbox adapter is actually attached, which is the one
    fact that predicts whether a message will wake it. Observed subscribers
    ⇒ :data:`ALIVE`, and nothing outranks it.

    A delivery observation NEVER yields :data:`DEAD`. Zero subscribers means a
    detached inbox adapter, not a corpse — the doctrine :mod:`.._listen.
    _reachability` was written to enforce ("``UNREACHABLE`` must NEVER be wired
    to anything destructive") — so it degrades to :data:`UNKNOWN` here.

``process`` / ``heartbeat`` / ``registry``
    HINTS. They corroborate; they may promote a verdict to :data:`UNKNOWN`;
    a *positive* one (a live pane pid, a fresh beat) is real evidence of life
    and does yield :data:`ALIVE`. But a NEGATIVE one on its own is never
    enough to destroy anything, because each is a shadow of the agent rather
    than the agent itself.

``registry`` in particular is a DECLARATION, not an observation — a row in a
table someone wrote once. It can vouch for a corpse, and it can be missing for
a healthy agent (on this fleet, active rows routinely carry ``pid = NULL``).
It is evidence text and corroboration only; it is never, by itself, either
pole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "ALIVE",
    "DEAD",
    "UNKNOWN",
    "SOURCE_DELIVERY",
    "SOURCE_HEARTBEAT",
    "SOURCE_PROCESS",
    "SOURCE_REGISTRY",
    "LivenessVerdict",
    "Signal",
    "decide",
]

# The three states. There is no fourth, and there is deliberately no bool.
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# Signal sources, in descending evidential strength.
SOURCE_DELIVERY = "delivery"  # the broker OBSERVED the agent's inbox. Authoritative.
SOURCE_PROCESS = "process"  # a process/session probe (pane pid, apptainer pid).
SOURCE_HEARTBEAT = "heartbeat"  # the agent's own writer refreshed its beat file.
SOURCE_REGISTRY = "registry"  # a row in a table. A declaration, not an observation.

# How many INDEPENDENT sources must agree on DEAD before a caller is allowed
# to destroy anything. Two, because every single signal in this system has
# been observed lying at least once, and the cost of being wrong is the
# destruction of a healthy agent. One dissenting ALIVE vetoes regardless.
_CORROBORATION_REQUIRED = 2


@dataclass(frozen=True)
class Signal:
    """One observation about one agent.

    ``verdict`` is ternary — :data:`ALIVE`, :data:`DEAD` or :data:`UNKNOWN`.
    A probe that could not run MUST report :data:`UNKNOWN` with the reason in
    ``detail``; it must never report :data:`DEAD`, because "I did not see it"
    and "I saw that it is not there" are different facts and only the second
    one may be acted on.

    ``detail`` is operator-facing and is expected to be specific: not
    "unhealthy" but ``pid=0 in heartbeat.json — no verdict possible`` or
    ``no tmux session 'tui-grant' (probe succeeded)``. A row that reads
    ``running | pid=None`` teaches an operator nothing; that is the UX bug
    this field exists to fix.
    """

    source: str
    verdict: str
    detail: str

    def __post_init__(self) -> None:
        if self.verdict not in (ALIVE, DEAD, UNKNOWN):
            raise ValueError(
                f"Signal.verdict must be one of {ALIVE!r} / {DEAD!r} / "
                f"{UNKNOWN!r}, got {self.verdict!r}. A liveness signal is "
                f"never a bool — 'I could not tell' is a first-class answer "
                f"and must be spelled {UNKNOWN!r}, never collapsed into a pole."
            )

    def to_dict(self) -> dict:
        return {"source": self.source, "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True)
class LivenessVerdict:
    """The resolved verdict for one agent, and the evidence that produced it.

    Do not construct this with a hand-picked ``verdict`` — go through
    :func:`decide`, so the rules hold by construction.
    """

    agent: str
    verdict: str
    signals: tuple[Signal, ...] = field(default=())

    @property
    def is_alive(self) -> bool:
        return self.verdict == ALIVE

    @property
    def is_dead(self) -> bool:
        return self.verdict == DEAD

    @property
    def is_unknown(self) -> bool:
        return self.verdict == UNKNOWN

    @property
    def dead_sources(self) -> tuple[str, ...]:
        """The DISTINCT sources that independently observed death."""
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == DEAD and sig.source not in seen:
                seen.append(sig.source)
        return tuple(seen)

    @property
    def alive_sources(self) -> tuple[str, ...]:
        """The DISTINCT sources that independently observed life."""
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == ALIVE and sig.source not in seen:
                seen.append(sig.source)
        return tuple(seen)

    @property
    def may_destroy(self) -> bool:
        """May a caller DESTROY this agent (kill / --force / restart) on this?

        ``True`` requires ALL of:

        * the verdict is :data:`DEAD` — never :data:`UNKNOWN`, and obviously
          never :data:`ALIVE`;
        * at least :data:`_CORROBORATION_REQUIRED` INDEPENDENT sources observed
          it dead — one signal is never enough, because each of them has been
          caught lying;
        * NOT ONE signal observed it alive. A single dissenting ALIVE vetoes
          destruction outright, even against a pile of DEADs. That asymmetry is
          deliberate: the cost of a false DEAD is a destroyed healthy agent,
          the cost of a false ALIVE is a report that says "I am not sure".

        This gates AUTOMATED destruction — watchdogs, health monitors, the
        listen's auto-restart. It is NOT a veto over the operator: an explicit
        ``--force`` remains the human override, which is exactly the escape
        hatch that makes a rule this strict safe to have.
        """
        if self.verdict != DEAD:
            return False
        if self.alive_sources:
            return False
        return len(self.dead_sources) >= _CORROBORATION_REQUIRED

    @property
    def destroy_veto_reason(self) -> str:
        """Why destruction is refused, in words, or ``""`` when it is allowed.

        Callers that decline to act should print THIS rather than inventing a
        message — so the operator learns which evidence was missing and can
        supply the explicit override knowingly.
        """
        if self.may_destroy:
            return ""
        if self.verdict == ALIVE:
            return (
                f"{self.agent} is ALIVE ({', '.join(self.alive_sources)}) — "
                f"refusing to destroy a live agent"
            )
        if self.verdict == UNKNOWN:
            return (
                f"{self.agent} liveness is UNKNOWN — no signal observed it "
                f"either alive or dead. UNKNOWN authorises nothing destructive: "
                f"a probe that could not run is not evidence of death"
            )
        if self.alive_sources:
            return (
                f"{self.agent} reads DEAD on {', '.join(self.dead_sources)} but "
                f"{', '.join(self.alive_sources)} observed it ALIVE — a single "
                f"live signal vetoes destruction"
            )
        return (
            f"{self.agent} reads DEAD on only {len(self.dead_sources)} source "
            f"({', '.join(self.dead_sources) or 'none'}); "
            f"{_CORROBORATION_REQUIRED} independent sources are required before "
            f"anything destructive is authorised"
        )

    def render(self) -> str:
        """One line: the verdict AND why. Never just the verdict.

        e.g. ``ALIVE (delivery: 1 live inbox subscriber)``
             ``UNKNOWN (heartbeat: pid=0 — no verdict possible)``
             ``DEAD (process: no tmux session 'tui-x'; registry: no active row)``

        An operator staring at ``running | pid=None`` learns nothing. This is
        the line that replaces it.
        """
        if not self.signals:
            return f"{self.verdict.upper()} (no signals gathered)"
        # Lead with the signals that AGREE with the verdict — that is the
        # reasoning; the dissenters follow so they are never hidden.
        agreeing = [s for s in self.signals if s.verdict == self.verdict]
        dissenting = [s for s in self.signals if s.verdict != self.verdict]
        parts = [f"{s.source}: {s.detail}" for s in agreeing]
        why = "; ".join(parts) if parts else "no corroborating signal"
        out = f"{self.verdict.upper()} ({why})"
        if dissenting:
            other = "; ".join(
                f"{s.source}[{s.verdict}]: {s.detail}" for s in dissenting
            )
            out += f" | also: {other}"
        return out

    def to_dict(self) -> dict:
        """JSON shape for ``--json`` consumers.

        ``evidence`` is always present, even when empty: a consumer must never
        have to guess whether the absence of evidence means "clean" or "we did
        not look".
        """
        return {
            "verdict": self.verdict,
            "summary": self.render(),
            "may_destroy": self.may_destroy,
            "destroy_veto_reason": self.destroy_veto_reason,
            "evidence": [s.to_dict() for s in self.signals],
        }


def decide(agent: str, signals: Iterable[Signal] | Sequence[Signal]) -> LivenessVerdict:
    """Fold observations into a verdict. Pure — no IO, no clock, no guessing.

    The rules, in order:

    1. **Any ALIVE ⇒ ALIVE.** Positive evidence of life is never overruled by
       the absence of other evidence. An agent that fails four proxy checks and
       answers one real one is a running agent.
    2. **Else any DEAD ⇒ DEAD.** Death needs POSITIVE evidence — a probe that
       actually ran and actually found nothing.
    3. **Else UNKNOWN.** No observation either way. This is a real answer, and
       the ONE thing a caller may not do with it is destroy something.

    Note what is absent: there is no path from "we gathered nothing" to
    :data:`DEAD`. That path is the bug.
    """
    sigs = tuple(signals)
    for sig in sigs:
        if sig.verdict == ALIVE:
            return LivenessVerdict(agent=agent, verdict=ALIVE, signals=sigs)
    for sig in sigs:
        if sig.verdict == DEAD:
            return LivenessVerdict(agent=agent, verdict=DEAD, signals=sigs)
    return LivenessVerdict(agent=agent, verdict=UNKNOWN, signals=sigs)
