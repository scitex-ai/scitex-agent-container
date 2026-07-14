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

The evidence VOCABULARY — the three states, the SOURCE_* reporters, the
INSTRUMENT_* sensors and :class:`Signal` — lives in
:mod:`._verdict_instruments` and is re-exported here, so every existing
``from ._verdict import ...`` keeps working. This module is the RULE.

Four rules, and they are the whole module
-----------------------------------------
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

4. **Corroboration is counted in INSTRUMENTS, not in reports.** Two signals
   from one sensor are ONE witness. Until 2026-07-14 this gate counted source
   STRINGS, so ``process`` + ``registry`` — which are the same
   ``os.kill(pid, 0)`` on the same pid, *by explicit design in both runtimes* —
   satisfied "2 independent sources" between them. A single syscall in the
   wrong pid namespace could therefore authorise killing a healthy agent. See
   :mod:`._verdict_instruments`.

What each signal is worth
-------------------------
``delivery``
    The ONLY authoritative signal. The broker inside ``sac listen`` knows
    whether an agent's inbox adapter is actually attached, which is the one
    fact that predicts whether a message will wake it. Observed subscribers
    ⇒ :data:`ALIVE`, and nothing outranks it.

    A delivery observation NEVER yields :data:`DEAD` — that is now ENFORCED by
    its :class:`._verdict_instruments.InstrumentSpec`, not merely asked for in
    prose. Zero subscribers means a detached inbox adapter, not a corpse.

``process`` / ``heartbeat`` / ``registry``
    HINTS. They corroborate; they may promote a verdict to :data:`UNKNOWN`;
    a *positive* one (a live pane pid, a fresh beat) is real evidence of life
    and does yield :data:`ALIVE`. But a NEGATIVE one on its own is never
    enough to destroy anything, because each is a shadow of the agent rather
    than the agent itself — and because two of them are frequently THE SAME
    SHADOW.

``registry`` in particular is a DECLARATION, not an observation — a row in a
table someone wrote once. It can vouch for a corpse, and it can be missing for
a healthy agent (on this fleet, active rows routinely carry ``pid = NULL``).
It is evidence text and corroboration only; it is never, by itself, either
pole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ._verdict_instruments import (
    ALIVE,
    CONVICTING_INSTRUMENTS,
    DEAD,
    INSTRUMENT_AGENT_SELF,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_INDEPENDENCE,
    INSTRUMENT_LISTEN_BROKER,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_PID_NAMESPACE,
    INSTRUMENTS,
    SOURCE_DELIVERY,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    SOURCE_RESOLVER,
    UNKNOWN,
    InstrumentSpec,
    Signal,
)

__all__ = [
    "ALIVE",
    "DEAD",
    "UNKNOWN",
    "SOURCE_DELIVERY",
    "SOURCE_HEARTBEAT",
    "SOURCE_PROCESS",
    "SOURCE_REGISTRY",
    "SOURCE_RESOLVER",
    "INSTRUMENT_AGENT_SELF",
    "INSTRUMENT_HOST_TMUX",
    "INSTRUMENT_LISTEN_BROKER",
    "INSTRUMENT_NO_OBSERVATION",
    "INSTRUMENT_PID_NAMESPACE",
    "INSTRUMENTS",
    "CONVICTING_INSTRUMENTS",
    "INSTRUMENT_INDEPENDENCE",
    "InstrumentSpec",
    "LivenessVerdict",
    "Signal",
    "decide",
]

# How many INDEPENDENT INSTRUMENTS must agree on DEAD before a caller is allowed
# to destroy anything. Two, because every single signal in this system has been
# observed lying at least once, and the cost of being wrong is the destruction
# of a healthy agent. One dissenting ALIVE vetoes regardless.
#
# NOTE WHAT THIS COUNTS: instruments. Not signals, not sources. Four reports
# from one sensor are ONE witness.
_CORROBORATION_REQUIRED = 2


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
        """The DISTINCT sources that REPORTED death.

        Reporters, not sensors — two of these can be one instrument, so this
        feeds the operator-facing text only. :attr:`may_destroy` counts
        :attr:`dead_instruments`.
        """
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == DEAD and sig.source not in seen:
                seen.append(sig.source)
        return tuple(seen)

    @property
    def alive_sources(self) -> tuple[str, ...]:
        """The DISTINCT sources that reported life."""
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == ALIVE and sig.source not in seen:
                seen.append(sig.source)
        return tuple(seen)

    @property
    def dead_instruments(self) -> tuple[str, ...]:
        """The DISTINCT SENSORS that independently observed death.

        THIS is what corroboration means. ``process`` and ``registry`` are two
        reporters but ONE instrument (the same ``os.kill(pid, 0)`` on the same
        pid, by explicit design in both runtimes), so they are ONE witness here.
        That is the entire point of the field.
        """
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == DEAD and sig.instrument not in seen:
                seen.append(sig.instrument)
        return tuple(seen)

    @property
    def alive_instruments(self) -> tuple[str, ...]:
        """The DISTINCT SENSORS that observed life."""
        seen: list[str] = []
        for sig in self.signals:
            if sig.verdict == ALIVE and sig.instrument not in seen:
                seen.append(sig.instrument)
        return tuple(seen)

    @property
    def may_destroy(self) -> bool:
        """May a caller DESTROY this agent (kill / --force / restart) on this?

        ``True`` requires ALL of:

        * the verdict is :data:`DEAD` — never :data:`UNKNOWN`, and obviously
          never :data:`ALIVE`;
        * at least :data:`_CORROBORATION_REQUIRED` INDEPENDENT INSTRUMENTS
          observed it dead. **Instruments, not signals and not sources.** Two
          reports from one sensor are one witness — and until 2026-07-14 this
          gate counted them as two, which meant a single ``os.kill(pid, 0)``
          evaluated in the wrong pid namespace could authorise killing a
          perfectly healthy agent;
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
        return len(self.dead_instruments) >= _CORROBORATION_REQUIRED

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
        if len(self.dead_sources) >= _CORROBORATION_REQUIRED:
            # The hole this gate shipped with: enough REPORTS, but they all came
            # off ONE sensor. Name it explicitly — an operator who sees "process
            # and registry both say dead" deserves to be told those are the same
            # os.kill(pid, 0) on the same pid, not two witnesses.
            return (
                f"{self.agent} reads DEAD on {len(self.dead_sources)} sources "
                f"({', '.join(self.dead_sources)}), but they share only "
                f"{len(self.dead_instruments)} INSTRUMENT "
                f"({', '.join(self.dead_instruments) or 'none'}) — that is one "
                f"sensor reported twice, not corroboration. "
                f"{_CORROBORATION_REQUIRED} INDEPENDENT instruments are required "
                f"before anything destructive is authorised"
            )
        return (
            f"{self.agent} reads DEAD on only {len(self.dead_instruments)} "
            f"instrument ({', '.join(self.dead_instruments) or 'none'}); "
            f"{_CORROBORATION_REQUIRED} independent instruments are required "
            f"before anything destructive is authorised"
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
        not look". ``dead_instruments`` is present so a consumer can see WHY a
        pile of DEAD reports did (or did not) authorise anything.
        """
        return {
            "verdict": self.verdict,
            "summary": self.render(),
            "may_destroy": self.may_destroy,
            "destroy_veto_reason": self.destroy_veto_reason,
            "dead_instruments": list(self.dead_instruments),
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

    Note also what this does NOT decide: whether the DEAD may be ACTED on. A
    DEAD verdict is a report; :attr:`LivenessVerdict.may_destroy` is the gate,
    and it counts INSTRUMENTS.
    """
    sigs = tuple(signals)
    for sig in sigs:
        if sig.verdict == ALIVE:
            return LivenessVerdict(agent=agent, verdict=ALIVE, signals=sigs)
    for sig in sigs:
        if sig.verdict == DEAD:
            return LivenessVerdict(agent=agent, verdict=DEAD, signals=sigs)
    return LivenessVerdict(agent=agent, verdict=UNKNOWN, signals=sigs)
