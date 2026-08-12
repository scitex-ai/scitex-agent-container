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

2. **Positive evidence of life is never overruled — but PRESENCE is not life.**
   If a signal observed the agent ALIVE, the verdict is :data:`ALIVE` — whatever
   the other signals say. This is not a heuristic, it is a lesson: an earlier
   ``pid AND port AND session_id`` predicate was refuted by ``scitex-writer``,
   which carried a stale ``startup_failed`` status, an unbound port and a null
   session_id — and was answering messages. Every one of those "dead" signals
   was a PROXY. The one that observed the agent itself said alive. Proxies
   lose.

   The refinement WEDGED adds: a ``process`` / ``heartbeat`` / ``registry``
   ALIVE observes only that the pid EXISTS — the session is up, the pane pid is
   alive — NOT that the agent is WORKING. So those PRESENCE-ALIVEs are overruled
   by a :data:`._verdict_instruments.WEDGED` from the screen instrument, which
   read the pane's CONTENT and found a frozen auth banner (the ``scitex-clew``
   auth-death that sat GREEN for two days). The one ALIVE that is real life, not
   mere presence — a ``delivery`` ALIVE, the broker seeing the inbox reachable —
   still wins outright. Presence loses to WEDGED; life does not.

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
    INSTRUMENT_TUI_SCREEN,
    INSTRUMENTS,
    SOURCE_DELIVERY,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    SOURCE_RESOLVER,
    SOURCE_SCREEN,
    UNKNOWN,
    WEDGED,
    InstrumentSpec,
    Signal,
    delivery_alive_wins,
)

__all__ = [
    "ALIVE",
    "DEAD",
    "UNKNOWN",
    "WEDGED",
    "SOURCE_DELIVERY",
    "SOURCE_HEARTBEAT",
    "SOURCE_PROCESS",
    "SOURCE_REGISTRY",
    "SOURCE_RESOLVER",
    "SOURCE_SCREEN",
    "INSTRUMENT_AGENT_SELF",
    "INSTRUMENT_HOST_TMUX",
    "INSTRUMENT_LISTEN_BROKER",
    "INSTRUMENT_NO_OBSERVATION",
    "INSTRUMENT_PID_NAMESPACE",
    "INSTRUMENT_TUI_SCREEN",
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


def _first_clause(detail: str) -> str:
    """The claim at the head of a verbose signal detail, without the lecture.

    Signal details lead with the OBSERVATION and put the (important, long)
    justification after an em-dash or a semicolon — e.g. ``"tmux probe
    SUCCEEDED and the server has NO session — positive evidence of absence,
    from tmux's own bookkeeping"``. A one-line status wants only that head;
    :meth:`LivenessVerdict.to_dict` keeps the whole thing for ``--json``.
    Splits on whichever of ``—`` / ``;`` comes first.
    """
    cut = len(detail)
    for sep in ("—", ";"):
        i = detail.find(sep)
        if i != -1:
            cut = min(cut, i)
    return detail[:cut].strip() or detail.strip()


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
        # UNCHANGED, and load-bearing: is_alive is ``verdict == ALIVE``, so a
        # WEDGED verdict reads is_alive False BY CONSTRUCTION. That is the whole
        # point — an auth-dead agent that a pid/session proxy calls ALIVE now
        # resolves to WEDGED, and WEDGED is not ALIVE.
        return self.verdict == ALIVE

    @property
    def is_dead(self) -> bool:
        return self.verdict == DEAD

    @property
    def is_unknown(self) -> bool:
        return self.verdict == UNKNOWN

    @property
    def is_wedged(self) -> bool:
        """PRESENT but not WORKING — a frozen auth banner, not a corpse.

        Distinct from every pole: is_alive / is_dead / is_unknown are all False
        for a wedged agent. It is not ALIVE (it is not working), not DEAD (the
        process is present, so destruction is not authorised), and not UNKNOWN
        (we DID observe it — we observed that it is stuck).
        """
        return self.verdict == WEDGED

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
        if self.verdict == WEDGED:
            return (
                f"{self.agent} is WEDGED — present but not working (a frozen "
                f"auth banner sits above its prompt). A restart clears a "
                f"rotated/stale token; destruction is NOT authorised, because "
                f"the process is alive and a wedged agent is not a corpse"
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
        """One line: the verdict AND why — TERSE. The full prose is in --json.

        e.g. ``ALIVE (delivery: 1 live inbox subscriber)``
             ``UNKNOWN (heartbeat: pid=0 in heartbeat.json)``
             ``DEAD (process: tmux has no session for this agent) | also: heartbeat[alive] | 2 signals had no reading``

        Each signal's ``detail`` is deliberately verbose and educational — the
        justification an operator can read once and learn from. But a STATUS
        line must not dump all of it for every signal at once; that is the
        "prose splurge" an operator rightly calls unreadable. So this shows the
        FIRST CLAUSE (the claim, before the lecture) of each AGREEING signal and
        reduces the dissenters to ``source[verdict]`` tags. The complete
        reasoning for every signal is always one ``--json`` away in
        :meth:`to_dict` (``evidence[].detail``) — nothing is lost, only folded.

        AN ABSTENTION IS NOT A DISSENT, and conflating the two is what the
        operator called dirty. Every non-agreeing signal used to be tagged the
        same way, so a routine ``sac-restart`` printed::

            DEAD (process: tmux probe SUCCEEDED and the tmux server has NO
            session for this agent) | also: delivery[unknown],
            heartbeat[unknown], registry[unknown], screen[unknown]

        Four tags, four times the same non-statement. An UNKNOWN signal holds
        no opinion to disagree with — it did not observe anything — so listing
        each by name spends the reader's attention on the instruments that had
        nothing to say, and buries any instrument that genuinely DISAGREES in
        the same list.

        The count is NOT dropped, because it is real evidence about the
        verdict's STRENGTH: DEAD on one instrument with four abstentions is a
        much weaker reading than DEAD on one with four agreeing, and the reader
        must be able to see that at a glance. So abstentions fold to a count
        and genuine opposition is always named, never folded — that tag is the
        one that vetoes destruction, and it must not be crowded out by silence.
        """
        if not self.signals:
            return f"{self.verdict.upper()} (no signals gathered)"
        # Lead with the signals that AGREE with the verdict — that is the
        # reasoning; the dissenters follow as tags so they are never hidden.
        agreeing = [s for s in self.signals if s.verdict == self.verdict]
        dissenting = [s for s in self.signals if s.verdict != self.verdict]
        parts = [f"{s.source}: {_first_clause(s.detail)}" for s in agreeing]
        why = "; ".join(parts) if parts else "no corroborating signal"
        out = f"{self.verdict.upper()} ({why})"
        # Split the non-agreeing signals by whether they actually hold a
        # contrary opinion. (When the verdict IS unknown, the abstainers agree
        # with it, so this list is empty by construction.)
        opposing = [s for s in dissenting if s.verdict != UNKNOWN]
        abstaining = [s for s in dissenting if s.verdict == UNKNOWN]
        if opposing:
            tags = ", ".join(f"{s.source}[{s.verdict}]" for s in opposing)
            out += f" | also: {tags}"
        if abstaining:
            noun = "signal" if len(abstaining) == 1 else "signals"
            out += f" | {len(abstaining)} {noun} had no reading"
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

    The rules, in strict order:

    1. **A ``delivery`` ALIVE is authoritative ⇒ ALIVE.** The broker OBSERVED the
       agent's inbox adapter attached — the one signal that watched a message be
       able to WAKE the agent, not a shadow of it. This is FIRST so it beats even
       WEDGED (rule 2). ONE exception, withdrawn in :func:`delivery_alive_wins`:
       a refusal WEDGE — the agent's own record that its last turn did not run.
    2. **Any WEDGED ⇒ WEDGED.** The screen instrument read the pane's rendered
       CONTENT and found a FROZEN auth banner above the prompt: the agent is
       PRESENT but NOT WORKING. This OVERRULES a ``process`` / ``heartbeat`` /
       ``registry`` ALIVE, because every one of those observes only that the pid
       EXISTS (the tmux session is up, the pane pid is alive) — none of them
       looks at whether the agent can actually do anything. That gap is the
       false-green this whole change exists to close: ``scitex-clew`` sat
       auth-dead for two days behind a pid-shaped ALIVE. Why a delivery-ALIVE
       (rule 1) still beats WEDGED: a broker-reachable agent is demonstrably
       working, the false-red cost of flagging it would be high, and the founding
       incident had delivery != ALIVE (the wedged agent's inbox was NOT
       observed reachable), so ordering delivery above WEDGED costs the fix
       nothing while keeping a live, answering agent from ever reading wedged.
    3. **Else any LIVE ALIVE ⇒ ALIVE** (the #705 stale-heartbeat carve-out is
       preserved here verbatim). Positive evidence of life is never overruled by
       the ABSENCE of other evidence: an agent that fails four proxy checks and
       answers one real one is running. The lone exception is a heartbeat ALIVE
       contradicted by a LIVE probe of the SAME instrument — a TUI beat merely
       re-reports the tmux snapshot ``process_signal`` reads live, so a live "no
       such session" (DEAD) ages that beat out. (A delivery ALIVE already
       returned in rule 1 and is never reached here.)
    4. **Else any DEAD ⇒ DEAD.** Death needs POSITIVE evidence — a probe that
       actually ran and actually found nothing.
    5. **Else UNKNOWN.** No observation either way. This is a real answer, and
       the ONE thing a caller may not do with it is destroy something.

    Note what is absent: there is no path from "we gathered nothing" to
    :data:`DEAD`. That path is the bug.

    Note also what this does NOT decide: whether the DEAD may be ACTED on. A
    DEAD verdict is a report; :attr:`LivenessVerdict.may_destroy` is the gate,
    and it counts INSTRUMENTS. A WEDGED verdict authorises nothing destructive
    either — ``may_destroy`` gates on DEAD, and WEDGED is not DEAD.
    """
    sigs = tuple(signals)
    # (1) A delivery ALIVE is a LIVE observation of the one thing that matters —
    # can a message reach this agent — and it is supreme, ABOVE even a WEDGED. It
    # is pulled out of the general positive-life step (rule 3) purely so it sits
    # ahead of rule 2.
    if delivery_alive_wins(sigs):
        return LivenessVerdict(agent=agent, verdict=ALIVE, signals=sigs)
    # (2) Any WEDGED wins over a pid/session ALIVE. Those proxies see only that
    # the process EXISTS; the screen instrument saw that it is not WORKING. A
    # WEDGED that reaches here is already fresh + this-incarnation — all the
    # staleness / SUPERSEDED gating lives in ``screen_signal`` — so it is trusted
    # at face value.
    for sig in sigs:
        if sig.verdict == WEDGED:
            return LivenessVerdict(agent=agent, verdict=WEDGED, signals=sigs)
    # (3) The general positive-life step, WITH the #705 stale-heartbeat carve-out
    # intact. A heartbeat is a STALE ARTEFACT: a value some other loop wrote to a
    # file at a past beat, re-reporting an instrument it sampled THEN. For a TUI
    # agent it re-reports the very tmux snapshot process_signal probes LIVE — so
    # when a live probe of the SAME instrument now returns DEAD (the tmux server
    # positively has no such session), the older beat is out of date and must not
    # vouch for life: "now" supersedes "up to 600s ago" ON ONE INSTRUMENT. This
    # is the count-instruments-not-sources rule in the time dimension — one sensor
    # cannot be alive and dead at once, and its LIVE reading beats its own stale
    # echo. (A reboot left a <600s beat behind a vanished session; the fold read
    # ALIVE and sac-start refused to relaunch a genuinely dead agent until the
    # beat finally aged past 600s.)
    dead_instruments = {s.instrument for s in sigs if s.verdict == DEAD}
    for sig in sigs:
        if sig.verdict != ALIVE:
            continue
        if sig.source == SOURCE_HEARTBEAT and sig.instrument in dead_instruments:
            continue  # stale echo of an instrument a live probe just found DEAD
        return LivenessVerdict(agent=agent, verdict=ALIVE, signals=sigs)
    # (4) any DEAD ⇒ DEAD.
    for sig in sigs:
        if sig.verdict == DEAD:
            return LivenessVerdict(agent=agent, verdict=DEAD, signals=sigs)
    # (5) else UNKNOWN.
    return LivenessVerdict(agent=agent, verdict=UNKNOWN, signals=sigs)
