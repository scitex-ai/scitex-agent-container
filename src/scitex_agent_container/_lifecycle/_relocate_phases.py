"""The ordered, journaled phases of a relocation — so a crash RESUMES.

The operator asked the right question when this was designed (2026-08-07): "what
if it dies mid-way". A relocation touches two hosts and a shared store, so it
will eventually be interrupted somewhere. The answer is not to make the whole
thing atomic — it cannot be — but to order the steps around ONE atomic point and
journal every transition, so that wherever it stops, the state on disk says
where it stopped and re-running continues rather than restarts.

    PREFLIGHT       validate the target before touching anything
    SOURCE_DRAIN    source finishes in-flight work, stops taking new
    SOURCE_STOP     stop the source, VERIFY stopped
    TRANSPORT       copy the transcript across, VERIFY it on the target
    TARGET_STANDBY  start the target WITHOUT the lease; it runs read-only
    HANDSHAKE       target -> source round trip; the source must OBSERVE a reply
    HANDOVER        the lease moves source -> target  <- THE atomic point
    DONE            append the residency record — WHICH IS the host write

THE SOURCE STOPS BEFORE THE TARGET STARTS, AND THAT ORDER IS FORCED BY THE
TRANSCRIPT rather than chosen for tidiness. Two physical constraints pin it:

    a RUNNING source appends to its .jsonl while it is being read, so the copy
    ends mid-line. jsonl carries no trailer and no length, so a torn transcript
    is not detectably torn — it parses, it resumes, and the conversation just
    stops early. Hence SOURCE_STOP precedes TRANSPORT.

    a target that has already BOOTED owns its own session marker, and seeding
    over it would discard whatever it did (see `_session_carry`, first-boot-only).
    Hence TRANSPORT precedes TARGET_STANDBY.

Between them there is exactly one legal slot, and it is this one. An earlier
draft of this file ordered the target's standby first and the source's stop last,
which reads safer — the source keeps running until the target has proven itself —
and cannot carry a transcript at all. That version is what shipped before the
transport existed; the reorder is the transport's precondition, not a preference.

THE PRICE IS STATED RATHER THAN HIDDEN: from SOURCE_STOP onward the agent is
DOWN on both hosts until the target comes up, and an abort in that window leaves
it stopped. That is a real regression against the previous order and it is
accepted, because the alternative is not "no downtime" but "no downtime and a
silently truncated memory". The window is recoverable and named: nothing durable
has been written, the source's own transcript is untouched (it was COPIED, never
moved), and starting the source again restores the world exactly. `abort`'s
verdict says so, so nobody has to infer it.

WHERE AN AGENT LIVES IS STILL WRITTEN ONLY AT DONE. The source stopping early
does NOT transfer residency: a stopped source is still the owner of record until
the residency row is appended, so at no point do two hosts hold a claim to one
identity. That is the invariant the reorder had to preserve, and it is preserved
because stopping a process and owning a record are different things.

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

EVERY ADDED PHASE IS BEFORE HANDOVER, and that placement is the whole reason it
is safe to add: HANDSHAKE only sends messages and reads replies and TRANSPORT
only copies a file onto a host that is not yet serving, so neither moves write
authority and the reversible-before / forward-after asymmetry is unchanged. A new
phase placed AFTER the handover would have broken it, because :func:`abort` would
then be refused for a step that is trivially undoable.

TRANSPORT IS REVERSIBLE IN THE ONLY SENSE THAT MATTERS: it never modifies the
source. The transcript is COPIED, and anything already at the destination is
MOVED ASIDE to `.old/<ts>/` rather than overwritten, so an abort leaves both
hosts holding everything they held before — the target merely holds an extra
copy, which the next run overwrites by moving it aside again.

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
    "TRANSPORT",
    "abort",
    "advance",
    "begin",
    "is_past_no_return",
    "leaves_source_stopped",
    "resume_from",
]

PREFLIGHT: Final = "preflight"
SOURCE_DRAIN: Final = "source_drain"
SOURCE_STOP: Final = "source_stop"
TRANSPORT: Final = "transport"
TARGET_STANDBY: Final = "target_standby"
HANDSHAKE: Final = "handshake"
HANDOVER: Final = "handover"
DONE: Final = "done"

#: The ordered sequence. Index in this tuple IS the ordering relation — there is
#: no second place where the order is written, so the two cannot disagree.
PHASES: Final[tuple[str, ...]] = (
    PREFLIGHT,
    SOURCE_DRAIN,
    SOURCE_STOP,
    TRANSPORT,
    TARGET_STANDBY,
    HANDSHAKE,
    HANDOVER,
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


def leaves_source_stopped(relocation: Relocation) -> bool:
    """True when abandoning HERE leaves the agent DOWN on the source host.

    The cost of ordering SOURCE_STOP before the transport (see the module
    docstring). It is a predicate rather than a comment because the recovery
    instruction depends on it: from SOURCE_STOP onward, "nothing was changed" is
    true of every durable record and false of the thing the operator will
    actually notice, and an abort that does not mention the stopped agent is a
    reassurance that sends nobody to restart it.

    ``ABORTED`` reports against the phase the relocation reached before it was
    abandoned — the journal's last real step — because after an abort the
    question being asked is precisely "what is still stopped".
    """
    phase = relocation.phase
    if phase == ABORTED:
        prior = [s.phase for s in relocation.steps if s.phase != ABORTED]
        phase = prior[-1] if prior else PREFLIGHT
    return PHASES.index(phase) >= PHASES.index(SOURCE_STOP)


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
    # Computed BEFORE the transition, against the phase actually reached: after
    # the replace() below the phase is ABORTED, and the question "is the agent
    # still running" is about where it got to, not about the abort.
    source_down = leaves_source_stopped(relocation)
    stopped = replace(
        relocation,
        phase=ABORTED,
        steps=relocation.steps + (Step(phase=ABORTED, at=now, detail=reason),),
    )
    note = (
        (
            f" {relocation.agent} is STOPPED on {relocation.from_host} and nothing "
            "restarted it — start it there to return to the pre-relocation state. "
            "Its own transcript was never moved, only copied, so it resumes intact."
        )
        if source_down
        else ""
    )
    return stopped, PhaseVerdict(
        allowed=True,
        code=CODE_OK,
        reason=f"{relocation.agent}: aborted at {relocation.phase} — {reason}.{note}",
    )
