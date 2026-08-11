"""The ordered, journaled phases of a relocation — so a crash RESUMES.

The operator asked the right question when this was designed (2026-08-07): "what
if it dies mid-way". A relocation touches two hosts and a shared store, so it
will eventually be interrupted somewhere. The answer is not to make the whole
thing atomic — it cannot be — but to order the steps around ONE atomic point and
journal every transition, so that wherever it stops, the state on disk says
where it stopped and re-running continues rather than restarts.

    PREFLIGHT       validate the target before touching anything
    TARGET_STANDBY  start the target WITHOUT the lease; it runs read-only
    HANDSHAKE       target -> source round trip; the source must OBSERVE a reply
    SOURCE_DRAIN    source finishes in-flight work, stops taking new
    HANDOVER        the lease moves source -> target  <- THE atomic point
    SOURCE_STOP     stop the source, VERIFY stopped
    DONE            append the residency record — WHICH IS the host write

THERE IS NO SPEC-EDITING PHASE, and its absence is a decision rather than an
omission (operator, 2026-08-11: 「設定ファイル、人が書くものはファイル、状態は
db」). A relocation writes NOTHING to a spec file. Where an agent actually runs
is an OBSERVATION, so the host lives in the state db and the residency record
appended at DONE *is* the write that moves it. A phase that edited spec.yaml
would have had a relocation modifying a git-tracked, human-authored file on two
machines whose copies are free to diverge — and it would have put an observation
back into the document that is supposed to hold only intent.

WHY THE ORDER IS THIS ORDER. Everything before HANDOVER is reversible and
everything after it is cleanup. Before: the source still holds the lease and the
target is a harmless read-only standby, so abandoning costs nothing. After: the
target holds the lease and the source is locked out by the bumped fence even
though its process is still alive, so the only sane direction is forward. A
crash on either side of that single point leaves exactly ONE writer — which is
the property the whole sequence exists to produce (see :mod:`_relocate_lease`).

THE ADDED PHASE IS BEFORE HANDOVER, and that placement is the whole reason it is
safe to add: HANDSHAKE only sends messages and reads replies, so it moves no
write authority and the reversible-before / forward-after asymmetry is
unchanged. A new phase placed AFTER the handover would have broken it, because
:func:`abort` would then be refused for a step that is trivially undoable.

WHY THE HANDSHAKE IS A PHASE AND NOT PART OF TARGET_STANDBY. "The process
started" and "the agent can do agent work" are different measurements, and
2026-08-11 showed the gap is real: a2a between two live agents delivered nothing
and nobody noticed until a human did. A relocation that folded the two together
would have declared the target ready on the strength of a running process, which
is the same "started, reported healthy, did nothing" shape the preflight checks
were written for. Separating them means the journal records WHICH of the two
failed, and a re-run resumes at the one that did.

That asymmetry is enforced here rather than documented: :func:`abort` REFUSES
once HANDOVER has happened. An abort at that stage would mean taking the lease
back from a target that already owns it, and the only honest way to do that is
another forward relocation, not an undo.

TRANSITIONS ARE ONE STEP FORWARD, NEVER BACKWARD, AND RE-ENTRY IS FREE.
Advancing to the phase you are ALREADY in is a no-op that succeeds, because a
coordinator that crashed after doing the work but before writing the journal
must be able to re-run without tripping over itself. Skipping a phase is
refused: each phase's precondition is the previous phase's completion, and a
relocation that jumped from PREFLIGHT to HANDOVER would move the lease to a
target nobody ever started.

Pure and fully injectable, exactly like the lease: no clock, no I/O, no store.
``now`` is passed in and a new immutable record is returned, so the persistence
layer stays a separate decision and the state machine is testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

__all__ = [
    "ABORTED",
    "CODE_ALREADY_THERE",
    "CODE_BACKWARD",
    "CODE_OK",
    "CODE_PAST_NO_RETURN",
    "CODE_SKIPS_PHASE",
    "CODE_TERMINAL",
    "CODE_UNKNOWN_PHASE",
    "DONE",
    "HANDOVER",
    "HANDSHAKE",
    "PHASES",
    "PREFLIGHT",
    "PhaseVerdict",
    "Relocation",
    "SOURCE_DRAIN",
    "SOURCE_STOP",
    "TARGET_STANDBY",
    "abort",
    "advance",
    "begin",
    "is_past_no_return",
    "resume_from",
]

PREFLIGHT: Final = "preflight"
TARGET_STANDBY: Final = "target_standby"
HANDSHAKE: Final = "handshake"
SOURCE_DRAIN: Final = "source_drain"
HANDOVER: Final = "handover"
SOURCE_STOP: Final = "source_stop"
DONE: Final = "done"

#: The ordered sequence. Index in this tuple IS the ordering relation — there is
#: no second place where the order is written, so the two cannot disagree.
PHASES: Final[tuple[str, ...]] = (
    PREFLIGHT,
    TARGET_STANDBY,
    HANDSHAKE,
    SOURCE_DRAIN,
    HANDOVER,
    SOURCE_STOP,
    DONE,
)

#: Not a phase — a terminal state reachable only from BEFORE the handover.
#: Deliberately outside PHASES so it can never be "advanced to" by the normal
#: one-step rule, and so ordering comparisons never have to special-case it.
ABORTED: Final = "aborted"

# Declared numeric codes with documented meanings, same discipline as the lease
# (see _relocate_lease): never a bare bool, never a small exit code carrying a
# domain meaning. Kept local rather than imported — a phase decision is not a
# lease decision, and sharing the constants would tie two vocabularies together
# for no gain beyond saving six lines.
CODE_OK: Final = 200
CODE_ALREADY_THERE: Final = 208
CODE_SKIPS_PHASE: Final = 400
CODE_UNKNOWN_PHASE: Final = 404
CODE_BACKWARD: Final = 409
CODE_PAST_NO_RETURN: Final = 410
CODE_TERMINAL: Final = 423
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class PhaseVerdict:
    """Whether a transition is permitted, in ONE shape.

    ``allowed`` is three-valued — ``True`` / ``False`` / ``None`` for "could not
    determine". Defines no ``__bool__`` on purpose, so ``if verdict:`` cannot
    read as permission for a refusal.
    """

    allowed: bool | None
    code: int
    reason: str

    def __post_init__(self) -> None:
        if self.allowed not in (True, False, None):
            raise ValueError(
                f"PhaseVerdict.allowed must be True/False/None, got {self.allowed!r}"
            )
        if not self.reason:
            raise ValueError(
                "PhaseVerdict.reason must be non-empty — a refusal with no reason is not actionable"
            )
        if self.allowed is True and self.code not in (CODE_OK, CODE_ALREADY_THERE):
            raise ValueError(
                f"PhaseVerdict: allowed=True must carry CODE_OK or CODE_ALREADY_THERE, got {self.code}"
            )
        if self.allowed is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"PhaseVerdict: allowed=None must carry CODE_UNKNOWN, got {self.code}"
            )


@dataclass(frozen=True)
class Step:
    """One journalled transition: which phase, when, and why."""

    phase: str
    at: float
    detail: str = ""


@dataclass(frozen=True)
class Relocation:
    """A relocation in flight, and the journal of how it got here.

    ``steps`` is append-only and includes the opening PREFLIGHT entry, so the
    record is self-describing: reading it tells you where the relocation is, how
    it arrived, and when each step happened — without consulting a log that may
    have rotated away.
    """

    agent: str
    from_host: str
    to_host: str
    phase: str
    steps: tuple[Step, ...]

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("Relocation.agent must be non-empty")
        if not self.from_host or not self.to_host:
            raise ValueError("Relocation needs both from_host and to_host")
        if self.from_host == self.to_host:
            raise ValueError(
                f"Relocation from {self.from_host!r} to itself is not a relocation — "
                "nothing would move, and the handover would take the lease from and give it to one host"
            )
        if self.phase not in PHASES and self.phase != ABORTED:
            raise ValueError(
                f"Relocation.phase {self.phase!r} is not a known phase; expected one of {PHASES} or {ABORTED!r}"
            )
        if not self.steps:
            raise ValueError("Relocation.steps must record at least the opening step")

    @property
    def started_at(self) -> float:
        return self.steps[0].at

    @property
    def is_terminal(self) -> bool:
        return self.phase in (DONE, ABORTED)


def is_past_no_return(relocation: Relocation) -> bool:
    """True once the lease has moved — i.e. at or beyond HANDOVER.

    The one predicate everything asymmetric keys off. Kept as a function rather
    than a comparison at each call site so "which side of the atomic point are
    we on" has exactly one definition.
    """
    if relocation.phase == ABORTED:
        return False
    return PHASES.index(relocation.phase) >= PHASES.index(HANDOVER)


def begin(
    *, agent: str, from_host: str, to_host: str, now: float, detail: str = ""
) -> Relocation:
    """Open a relocation at PREFLIGHT. Touches nothing but this record."""
    return Relocation(
        agent=agent,
        from_host=from_host,
        to_host=to_host,
        phase=PREFLIGHT,
        steps=(Step(phase=PREFLIGHT, at=now, detail=detail or "relocation opened"),),
    )


def resume_from(relocation: Relocation) -> str | None:
    """The phase a re-run should execute next, or ``None`` if there is nothing
    left to do.

    A crashed coordinator asks this instead of starting over. Because a phase is
    only recorded once its work is done, the answer is always "the one after the
    last recorded phase" — and for a terminal relocation it is ``None``, which
    the caller must handle explicitly rather than looping.
    """
    if relocation.is_terminal:
        return None
    return PHASES[PHASES.index(relocation.phase) + 1]


def advance(
    relocation: Relocation,
    *,
    to_phase: str,
    now: float,
    detail: str = "",
) -> tuple[Relocation, PhaseVerdict]:
    """Record that ``to_phase`` has completed. One step forward only.

    Re-advancing to the CURRENT phase succeeds without appending a duplicate
    step (``CODE_ALREADY_THERE``): a coordinator that finished the work and died
    before journalling must be able to re-run harmlessly. Skipping ahead,
    stepping back, and moving a terminal relocation are all refused.
    """
    if to_phase not in PHASES:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_UNKNOWN_PHASE,
            reason=f"{to_phase!r} is not a phase; expected one of {PHASES}",
        )
    if relocation.is_terminal:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_TERMINAL,
            reason=f"relocation of {relocation.agent} is already {relocation.phase}; open a new one rather than reviving this",
        )
    here = PHASES.index(relocation.phase)
    there = PHASES.index(to_phase)
    if there == here:
        return relocation, PhaseVerdict(
            allowed=True,
            code=CODE_ALREADY_THERE,
            reason=f"already at {to_phase} — re-run is a no-op, nothing appended",
        )
    if there < here:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_BACKWARD,
            reason=f"cannot go back from {relocation.phase} to {to_phase}; a relocation only moves forward",
        )
    if there > here + 1:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_SKIPS_PHASE,
            reason=(
                f"cannot jump {relocation.phase} -> {to_phase}: each phase's precondition is the "
                f"previous one's completion, so the next legal step is {PHASES[here + 1]}"
            ),
        )
    moved = replace(
        relocation,
        phase=to_phase,
        steps=relocation.steps + (Step(phase=to_phase, at=now, detail=detail),),
    )
    return moved, PhaseVerdict(
        allowed=True,
        code=CODE_OK,
        reason=f"{relocation.agent}: {relocation.phase} -> {to_phase}",
    )


def abort(
    relocation: Relocation,
    *,
    now: float,
    reason: str,
) -> tuple[Relocation, PhaseVerdict]:
    """Abandon a relocation that has NOT yet handed over the lease.

    Refused at or past HANDOVER. There the target already owns the lease and the
    source is fenced out, so "undo" would mean taking write authority back from
    a live holder — which is itself a relocation, and should be run as one
    rather than smuggled in under a name that promises the opposite.
    """
    if not reason:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_SKIPS_PHASE,
            reason="an abort must state why — an unexplained abandonment is indistinguishable from a crash",
        )
    if relocation.phase == ABORTED:
        return relocation, PhaseVerdict(
            allowed=True,
            code=CODE_ALREADY_THERE,
            reason="already aborted — re-run is a no-op",
        )
    if relocation.phase == DONE:
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_TERMINAL,
            reason=f"relocation of {relocation.agent} completed; there is nothing to abort",
        )
    if is_past_no_return(relocation):
        return relocation, PhaseVerdict(
            allowed=False,
            code=CODE_PAST_NO_RETURN,
            reason=(
                f"cannot abort at {relocation.phase}: the lease already moved to {relocation.to_host}. "
                "Finish forward, or relocate back deliberately — do not undo a handover"
            ),
        )
    stopped = replace(
        relocation,
        phase=ABORTED,
        steps=relocation.steps + (Step(phase=ABORTED, at=now, detail=reason),),
    )
    return stopped, PhaseVerdict(
        allowed=True,
        code=CODE_OK,
        reason=f"{relocation.agent}: aborted at {relocation.phase} — {reason}",
    )
