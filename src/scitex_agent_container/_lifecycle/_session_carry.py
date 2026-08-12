"""Decide whether a conversation transcript should be carried to a new session.

`_twin.py` already knows HOW to do this — resolve a source's live session uuid,
write it as the target's ``session_id`` marker so ``session: continue`` resumes
it, copy the ``<uuid>.jsonl`` transcript into the target's projects store, and do
all of that ON FIRST BOOT ONLY. What it does not have is a way to ask the
question separately from doing the work, and `relocate` needs the same decision
with a different transport: twin copies within one host, relocate copies across
two.

So this module owns the DECISION and nothing else. The copy stays with whoever
can actually perform it.

WHY THIS IS NOT "relocate calls twin". The operator rejected that conflation
twice (2026-08-07), and the card records why: the two verbs differ on the axis
they change.

    relocate   WHERE it runs — identity unchanged, count 1 -> 1
    fork/twin  WHAT it does  — new identity, count 1 -> 2

They share exactly one implementation detail, which is this decision. Sharing
more would let one verb's lifecycle leak into the other's, and describing either
in terms of the other is how a word ends up meaning two things.

FIRST BOOT ONLY, and it is the rule that matters most. Once the target has its
OWN session marker it has booted and diverged; re-seeding would DISCARD that
history and re-fork from the source on every restart. So an existing marker is a
refusal, not an error — the correct outcome, stated as one.

Three-valued like the rest of the relocate machinery: ``True`` carry, ``False``
do not, ``None`` could not tell. An unknown here must never be read as "no" —
that would silently produce the exact defect the card was filed for, an agent
resuming with no memory of the conversation that moved it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CODE_ALREADY_DIVERGED",
    "CODE_CARRY",
    "CODE_NO_SOURCE_SESSION",
    "CODE_NO_TRANSCRIPT",
    "CODE_OPTED_OUT",
    "CODE_UNKNOWN",
    "SeedPlan",
    "plan_session_carry",
]

CODE_CARRY: Final = 200
CODE_ALREADY_DIVERGED: Final = 208
CODE_OPTED_OUT: Final = 204
CODE_NO_SOURCE_SESSION: Final = 404
CODE_NO_TRANSCRIPT: Final = 410
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class SeedPlan:
    """Whether to carry the transcript, and everything needed to do it.

    ``carry`` is three-valued. ``None`` means the inputs did not answer the
    question — distinct from a decided "no", because the two call for different
    behaviour: a decided no proceeds, an unknown must stop and find out.
    """

    carry: bool | None
    code: int
    reason: str
    session_uuid: str | None = None
    transcript_name: str | None = None

    def __post_init__(self) -> None:
        if self.carry not in (True, False, None):
            raise ValueError(
                f"SeedPlan.carry must be True/False/None, got {self.carry!r}"
            )
        if not self.reason:
            raise ValueError("SeedPlan.reason must be non-empty")
        if self.carry is True and self.code != CODE_CARRY:
            raise ValueError(
                f"SeedPlan: carry=True must carry CODE_CARRY, got {self.code}"
            )
        if self.carry is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"SeedPlan: carry=None must carry CODE_UNKNOWN, got {self.code}"
            )
        if self.carry is True and not self.session_uuid:
            raise ValueError(
                "SeedPlan: a carry plan must name the session uuid it will seed"
            )
        if self.carry is True and not self.transcript_name:
            raise ValueError(
                "SeedPlan: a carry plan must name the transcript file it will copy"
            )


def plan_session_carry(
    *,
    source_session_uuid: str | None,
    source_transcript_exists: bool | None,
    target_has_own_marker: bool | None,
    requested: bool = True,
) -> SeedPlan:
    """Decide, from observed facts alone. Copies nothing, reads nothing.

    ``requested`` is the caller's intent — for ``relocate`` it defaults to True
    (the same agent continuing SHOULD remember the conversation that moved it;
    ``--no-carry-session`` sets it False), and for a purpose-scoped fork the
    caller may pass False and supply a summary instead.

    Every fact is ``| None`` for NOT OBSERVED, which is deliberately distinct
    from an observed negative: a probe that failed must not be able to
    masquerade as "there is no transcript".
    """
    if not requested:
        return SeedPlan(
            carry=False,
            code=CODE_OPTED_OUT,
            reason=(
                "carry not requested — the target will start a fresh session with no memory "
                "of the conversation that moved it; this is only right when leaving a wedged "
                "session behind deliberately"
            ),
        )
    if target_has_own_marker is None:
        return SeedPlan(
            carry=None,
            code=CODE_UNKNOWN,
            reason=(
                "could not tell whether the target already has its own session marker; "
                "seeding over a diverged session would DISCARD its history, so find out "
                "before deciding"
            ),
        )
    if target_has_own_marker:
        return SeedPlan(
            carry=False,
            code=CODE_ALREADY_DIVERGED,
            reason=(
                "target already has its own session marker — it has booted and diverged. "
                "Re-seeding would discard that history and re-fork from the source on every "
                "restart; `continue` resumes the target's own latest session instead"
            ),
        )
    if source_session_uuid is None:
        return SeedPlan(
            carry=None,
            code=CODE_UNKNOWN,
            reason="the source's live session uuid was not observed; read its session_id marker before deciding",
        )
    if not source_session_uuid:
        return SeedPlan(
            carry=False,
            code=CODE_NO_SOURCE_SESSION,
            reason=(
                "the source has no live session, so there is no conversation to carry — "
                "the target will start fresh, which is correct here but worth saying out loud"
            ),
        )
    if source_transcript_exists is None:
        return SeedPlan(
            carry=None,
            code=CODE_UNKNOWN,
            reason=f"could not tell whether the source transcript for {source_session_uuid} exists; check before deciding",
        )
    if not source_transcript_exists:
        return SeedPlan(
            carry=False,
            code=CODE_NO_TRANSCRIPT,
            reason=(
                f"the source names session {source_session_uuid} but its transcript is missing — "
                "seeding a marker with no transcript produces an agent that resumes into nothing"
            ),
            session_uuid=source_session_uuid,
        )
    return SeedPlan(
        carry=True,
        code=CODE_CARRY,
        reason=f"carrying session {source_session_uuid} to the target's first boot",
        session_uuid=source_session_uuid,
        transcript_name=f"{source_session_uuid}.jsonl",
    )
