"""The driver: walk the phases, journal each one, and stop the moment one is not a yes.

Everything else in the relocate machinery decides. This is the piece that ACTS —
and its entire design problem is that it must act without ever inventing a
result. Each phase is an injected callable returning a three-valued
:class:`StepResult`; this module owns the ORDER, the journal, and the one rule
that makes the sequence safe:

    BEFORE the handover, a non-yes ABORTS and nothing persistent has changed.
    AT or AFTER it, a non-yes STOPS AND REPORTS — it never rolls back.

THERE IS NOTHING TO ROLL BACK BEFORE THE HANDOVER, and that is a property of the
phase order rather than luck. No pre-handover phase writes anything durable: the
target standby is a started process, the handshake is messages. The host is not
written until DONE, because where an agent runs is an OBSERVATION and the
residency record IS that write (operator, 2026-08-11: 「設定ファイル、人が書く
ものはファイル、状態は db」). An earlier draft had a phase that rewrote the
spec's ``host:`` and therefore needed an undo; removing it removed the only
reversible-but-durable step, which is why ``abort`` here has no compensation to
perform and none is offered.

That asymmetry is not a choice made here; it is enforced by
:func:`.._relocate_phases.abort`, which refuses at or past HANDOVER. This module
simply must not fight it: past that point the target already owns the lease and
the source is fenced out, so an "undo" would mean taking write authority back
from a live holder. The honest response to a failure there is to say exactly
where it stopped and what is now true, because the fix is forward.

UNKNOWN STOPS AS FIRMLY AS FAILURE, AND SAYS SOMETHING DIFFERENT. A step that
could not be measured has not succeeded. The two are reported apart because they
call for different actions — go and measure it, versus go and fix it — but
neither continues. This is where that discipline is most load-bearing: a step
whose unknown was folded into success would hand the lease to a target nobody
established was working, which is the 2026-08-07 failure with the source already
stopped.

WHY THE EFFECTS ARE INJECTED, GIVEN THAT THIS IS THE PART THAT DOES I/O. Because
the ORDER is what has to be tested, and the order is exactly what a two-host
integration test cannot pin reliably. With effects as callables, every path —
each phase failing, each phase unknown, a failure on either side of the atomic
point, a resume from a half-finished journal — is a test with real callables
returning real values, no mocks and no second machine. The CLI supplies the
adapters that actually ssh.

RE-ENTRY IS FREE, because :func:`.._relocate_phases.advance` makes it so:
advancing to the phase already recorded is a no-op that succeeds. A coordinator
that did the work and died before journalling re-runs harmlessly, which is the
only reason a crash "resumes" rather than restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Final

from ._relocate_phases import (
    DONE,
    HANDOVER,
    HANDSHAKE,
    PHASES,
    PREFLIGHT,
    SOURCE_DRAIN,
    SOURCE_STOP,
    TARGET_STANDBY,
    Relocation,
    abort,
    advance,
    is_past_no_return,
)

__all__ = [
    "CODE_ABORTED",
    "CODE_COMPLETED",
    "CODE_JOURNAL_REFUSED",
    "CODE_STOPPED_PAST_NO_RETURN",
    "CODE_UNKNOWN",
    "ExecuteOutcome",
    "PhaseEffects",
    "StepResult",
    "execute",
]

#: Every phase through DONE completed.
CODE_COMPLETED: Final = 200
#: A phase refused BEFORE the handover; the relocation was aborted and undone.
CODE_ABORTED: Final = 409
#: A phase refused AT or AFTER the handover. Not undone — cannot be.
CODE_STOPPED_PAST_NO_RETURN: Final = 410
#: The phase machine refused a transition (a skip, a backward step, a terminal
#: relocation). A coordinator bug rather than a host problem, and reported as
#: its own thing so the two are never confused.
CODE_JOURNAL_REFUSED: Final = 412
#: A step could not be measured. Refuses as firmly as a failure, differently.
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class StepResult:
    """What one phase's effect achieved, in the same three-valued shape as the rest.

    ``ok`` is ``True`` done, ``False`` it did not happen, ``None`` COULD NOT
    TELL. No ``__bool__``: ``if result:`` would be true for a refusal, and the
    step after this one may be the handover.

    ``detail`` lands in the journal, so it is the sentence a reader gets months
    later when asking what this relocation actually did. It is required for the
    same reason a refusal must carry a reason.
    """

    ok: bool | None
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.ok not in (True, False, None):
            raise ValueError(f"StepResult.ok must be True/False/None, got {self.ok!r}")
        if not self.detail:
            raise ValueError(
                "StepResult.detail must be non-empty — it is what the journal records, "
                "and an unexplained step is indistinguishable from one that never ran"
            )
        if self.ok is not True and not self.hint:
            raise ValueError(
                "StepResult: a step that did not succeed must carry a hint naming the next action"
            )


@dataclass
class PhaseEffects:
    """One callable per phase. There is no undo, because there is nothing to undo.

    Every phase after PREFLIGHT is required. An omitted effect is not treated as
    a skipped no-op: a relocation missing its handover would journal its way to
    DONE having moved nothing, which is the single most convincing way to
    produce the "looks exactly like success" failure this whole feature exists
    to prevent. :func:`execute` refuses to start without all of them.

    NO ROLLBACK IS OFFERED AND NONE IS NEEDED. No pre-handover phase writes
    anything durable — the standby is a started process and the handshake is
    messages — and the host is written by ``finish`` at DONE, past the point
    where an abort is legal at all. TARGET_STANDBY does leave a started standby
    behind on an abort, which is deliberate and is called out in the outcome
    rather than silently cleaned up: stopping it would be another remote action
    taken during an abort, at the moment least is known about the state of
    things.
    """

    start_target_standby: Callable[[], StepResult] | None = None
    handshake: Callable[[], StepResult] | None = None
    drain_source: Callable[[], StepResult] | None = None
    hand_over_lease: Callable[[], StepResult] | None = None
    stop_source: Callable[[], StepResult] | None = None
    finish: Callable[[], StepResult] | None = None

    def for_phase(self, phase: str) -> Callable[[], StepResult] | None:
        return {
            TARGET_STANDBY: self.start_target_standby,
            HANDSHAKE: self.handshake,
            SOURCE_DRAIN: self.drain_source,
            HANDOVER: self.hand_over_lease,
            SOURCE_STOP: self.stop_source,
            DONE: self.finish,
        }.get(phase)


@dataclass(frozen=True)
class ExecuteOutcome:
    """Where the relocation got to, and what is true of the world now.

    ``completed`` is three-valued: True every phase through DONE; False it
    stopped and the stop is understood; None it stopped because something could
    not be measured. ``relocation`` is the journal as it stands, so a re-run
    reads it and resumes.
    """

    completed: bool | None
    code: int
    reason: str
    relocation: Relocation
    stopped_at: str = ""
    hint: str = ""
    #: True when the target was started and then abandoned by an abort. Reported
    #: rather than cleaned up: stopping it would be another remote action taken
    #: at the moment least is known about the state of things.
    standby_left_running: bool = False
    log: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.completed not in (True, False, None):
            raise ValueError(
                f"ExecuteOutcome.completed must be True/False/None, got {self.completed!r}"
            )
        if not self.reason:
            raise ValueError("ExecuteOutcome.reason must be non-empty")
        if self.completed is True and self.code != CODE_COMPLETED:
            raise ValueError(
                f"ExecuteOutcome: completed=True must carry CODE_COMPLETED, got {self.code}"
            )
        if self.completed is not True and not self.hint:
            raise ValueError(
                "ExecuteOutcome: a relocation that did not complete must say what to do next"
            )

    @property
    def past_no_return(self) -> bool:
        return is_past_no_return(self.relocation)


def _missing_effects(effects: PhaseEffects) -> tuple[str, ...]:
    return tuple(p for p in PHASES if p != PREFLIGHT and effects.for_phase(p) is None)


def _standby_running(last_done: str, failing: str, unknown: bool) -> bool:
    """Whether an abort here leaves a started target behind.

    True once TARGET_STANDBY has completed, and also when the standby phase
    ITSELF returned UNKNOWN: an effect that could not say whether the target
    came up may well have started it, and reporting "nothing was left running"
    on that basis is the kind of reassurance that sends nobody to look.
    """
    if failing == TARGET_STANDBY:
        return unknown
    return PHASES.index(last_done) >= PHASES.index(TARGET_STANDBY)


def execute(
    relocation: Relocation,
    *,
    effects: PhaseEffects,
    now: Callable[[], float],
) -> ExecuteOutcome:
    """Drive ``relocation`` forward from wherever its journal says it is.

    ``now`` is a callable rather than a value because a relocation spans real
    time and each journalled step should carry the moment it actually happened —
    a single timestamp captured up front would make a twenty-minute sequence
    look instantaneous, and the journal's whole purpose is telling a later
    reader when things happened.

    Returns rather than raises for every expected outcome, including refusals:
    the caller needs the journal back in order to resume, and an exception
    carries a message where a resumable record is wanted.
    """
    missing = _missing_effects(effects)
    if missing:
        raise ValueError(
            f"refusing to execute: no effect supplied for {', '.join(missing)}. A phase "
            "with no effect would journal as done having changed nothing, which is the "
            "most convincing possible imitation of a successful relocation"
        )

    log: list[str] = []
    current = relocation

    for phase in PHASES[PHASES.index(current.phase) + 1 :]:
        effect = effects.for_phase(phase)
        assert effect is not None  # guaranteed by _missing_effects above
        result = effect()
        log.append(f"{phase}: {result.detail}")

        if result.ok is not True:
            unknown = result.ok is None
            if is_past_no_return(current):
                # The lease has already moved. Saying so is the useful part: the
                # target owns write authority and the source may still be
                # running, which is a state someone has to settle deliberately.
                return ExecuteOutcome(
                    completed=None if unknown else False,
                    code=CODE_UNKNOWN if unknown else CODE_STOPPED_PAST_NO_RETURN,
                    reason=(
                        f"{phase} did not complete AFTER the lease moved to "
                        f"{current.to_host}: {result.detail}"
                    ),
                    relocation=current,
                    stopped_at=phase,
                    hint=(
                        f"{result.hint} This cannot be rolled back — {current.to_host} "
                        f"holds the lease and {current.from_host} is fenced out. Finish "
                        "forward: re-run to resume from this phase"
                    ),
                    log=tuple(log),
                )
            stopped, verdict = abort(
                current,
                now=now(),
                reason=f"{phase}: {result.detail}",
            )
            left_running = _standby_running(current.phase, phase, unknown)
            return ExecuteOutcome(
                completed=None if unknown else False,
                code=CODE_UNKNOWN if unknown else CODE_ABORTED,
                reason=f"{phase} did not complete: {result.detail}",
                relocation=stopped if verdict.allowed is True else current,
                stopped_at=phase,
                hint=(
                    f"{result.hint} Nothing was handed over; the lease is still with "
                    f"{current.from_host}, and the host in the state db is unchanged."
                    + (
                        f" The standby on {current.to_host} was started and is STILL "
                        "RUNNING — stop it deliberately, or leave it for the re-run."
                        if left_running
                        else ""
                    )
                ),
                standby_left_running=left_running,
                log=tuple(log),
            )

        current, verdict = advance(
            current, to_phase=phase, now=now(), detail=result.detail
        )
        if verdict.allowed is not True:
            # The machine refused a transition we believed was legal. That is a
            # bug in this driver, not a fact about a host, so it is reported as
            # its own code rather than folded into a phase failure.
            return ExecuteOutcome(
                completed=False,
                code=CODE_JOURNAL_REFUSED,
                reason=f"the phase journal refused {current.phase} -> {phase}: {verdict.reason}",
                relocation=current,
                stopped_at=phase,
                hint=(
                    "the effect for this phase reported success but the journal would not "
                    "record it. Read the journal before re-running — the relocation may "
                    "have already progressed past this point in an earlier run"
                ),
                log=tuple(log),
            )

    return ExecuteOutcome(
        completed=True,
        code=CODE_COMPLETED,
        reason=(
            f"{current.agent}: {current.from_host} -> {current.to_host}, every phase "
            f"through {DONE} recorded"
        ),
        relocation=current,
        log=tuple(log),
    )
