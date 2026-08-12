"""Where an agent runs is OBSERVED, so the answer comes from the db — not the spec.

The operator settled this on 2026-08-11, after asking the sharp version of the
question (「spec がファイルか db か、迷うところ。」):

    設定ファイル、人が書くものはファイル、状態は db

``host`` was in the spec for years and it was never intent. A human typing
``host: nas-03`` is not declaring a preference, they are recording a fact — and
a fact recorded by hand in a git-tracked file that exists in two copies on two
machines is a fact that will eventually be wrong in at least one of them. So the
host moves to the state db, and this module is the single place that answers
"where does this agent live".

WHY A RELOCATION THEN NEEDS NO SPEC-EDITING PHASE. Writing the db IS the move.
:mod:`_residency` already records which host an agent lives on and when, with at
most one stay open at a time; appending to it at DONE is the whole of the
operator's item #1. An earlier draft of this work had a SPEC_REWRITE phase with
an undo; it is gone, not dormant.

THE MIGRATION, AND IT IS THE PART THAT MATTERS. Every spec on disk today carries
``host:``. The rule here is SEED ONCE, THEN IGNORE:

    the db knows the agent   -> the db wins, always. The spec's host: is not
                                consulted, not compared, and not warned about
                                on every read.
    the db knows nothing     -> the spec's host: seeds the db, ONCE, and the
                                seeding is recorded (``host_seeded_from_spec``)
                                so the value's provenance is not lost.
    neither knows            -> UNKNOWN. Not "local", not the current hostname.

The middle branch exists so that no agent has to be re-registered by hand; it is
not a fallback that keeps running. A field that is authoritative on Tuesday and
ignored on Wednesday is worse than either, so once the db has an answer the
spec's copy is dead text — and :func:`legacy_spec_host_notice` exists to say so
in the dry run rather than leaving the operator to infer it from silence.

WHAT THIS DOES NOT DO: guess. ``sac agents list`` prints ``host`` as the literal
string ``'local'`` on every row today, which is a placeholder standing where an
observation belongs, and it prints ``defined`` — a SPEC fact, "a file exists" —
in a STATE column called ``status``. Between them that is why the listing
reported zero agents running while two dozen were live. This module returns
``None`` when it does not know, and ``None`` must not be rendered as a hostname.

Pure: no db handle, no file read. The stored residency history comes in and an
answer goes out, so the resolution rule is testable without either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ._residency import Residency, current_host, open_residency

__all__ = [
    "CODE_FROM_DB",
    "CODE_SEEDED_FROM_SPEC",
    "CODE_UNKNOWN",
    "HostAnswer",
    "legacy_spec_host_notice",
    "record_move",
    "resolve_host",
]

#: The db knew. The ordinary answer, and the only one after the first seeding.
CODE_FROM_DB: Final = 200
#: The db knew nothing and a legacy spec ``host:`` seeded it, once.
CODE_SEEDED_FROM_SPEC: Final = 201
#: Nobody knows. NOT a hostname, NOT "local".
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class HostAnswer:
    """Where the agent runs, and — load-bearing — WHERE THAT CAME FROM.

    ``host`` is ``None`` for "not known", which callers must handle rather than
    render. ``seeded_from_spec`` records that this answer originated in a legacy
    spec field, so the provenance survives into the db row instead of the value
    arriving there looking like something that was measured.
    """

    host: str | None
    code: int
    reason: str
    seeded_from_spec: bool = False
    #: The history AFTER any seeding, for the caller to persist. Unchanged when
    #: nothing was seeded.
    history: tuple[Residency, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("HostAnswer.reason must be non-empty")
        if self.host is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"HostAnswer: an unknown host must carry CODE_UNKNOWN, got {self.code}"
            )
        if self.host is not None and not self.host.strip():
            raise ValueError(
                "HostAnswer.host must be a real hostname or None — a blank string "
                "renders as an answer while meaning nothing"
            )
        if self.seeded_from_spec and self.code != CODE_SEEDED_FROM_SPEC:
            raise ValueError(
                "HostAnswer: seeded_from_spec must carry CODE_SEEDED_FROM_SPEC"
            )


def resolve_host(
    history: tuple[Residency, ...],
    *,
    legacy_spec_host: str | None = None,
    now: float | None = None,
) -> HostAnswer:
    """Answer "where does this agent run" from the db, seeding once if it must.

    ``history`` is the agent's residency history as stored. ``legacy_spec_host``
    is the ``host:`` still sitting in an old spec file — passed in explicitly so
    that a caller has to decide to offer it, rather than this module reaching
    into a spec on its own.

    ``now`` is required to seed (a residency record without a start time is not
    a record); omitting it while a seed is needed yields UNKNOWN naming the
    reason, instead of inventing a timestamp.
    """
    known = current_host(history)
    if known:
        return HostAnswer(
            host=known,
            code=CODE_FROM_DB,
            reason=f"the state db has {known} as the open residency",
            history=history,
        )

    candidate = (legacy_spec_host or "").strip()
    if not candidate:
        return HostAnswer(
            host=None,
            code=CODE_UNKNOWN,
            reason=(
                "the state db has no open residency for this agent and no legacy "
                "spec host: was offered — where it runs is genuinely unknown. Record "
                "it with a residency before relying on it; do NOT substitute the "
                "local hostname, which is the guess that makes a split-brain look "
                "explained"
            ),
            history=history,
        )
    if now is None:
        return HostAnswer(
            host=None,
            code=CODE_UNKNOWN,
            reason=(
                f"the db knows nothing and the legacy spec says {candidate!r}, but no "
                "timestamp was supplied to open a residency with — a stay with no "
                "start time cannot answer an attribution question later"
            ),
            history=history,
        )
    return HostAnswer(
        host=candidate,
        code=CODE_SEEDED_FROM_SPEC,
        reason=(
            f"seeded {candidate!r} from the spec's legacy host: field, ONCE. From now "
            "on the db is the answer and that field is ignored"
        ),
        seeded_from_spec=True,
        history=open_residency(history, host=candidate, now=now),
    )


def record_move(
    history: tuple[Residency, ...], *, to_host: str, now: float
) -> tuple[Residency, ...]:
    """The item-#1 write: the agent now runs on ``to_host``.

    Delegates to :func:`.._residency.open_residency`, which closes the previous
    stay in the same step — so "living in two places at once" stays
    unrepresentable rather than merely discouraged — and is idempotent on a move
    already recorded, so a coordinator re-running after a crash does not litter
    the history with the evidence of its own retries.

    This is the ONLY write a relocation makes on the host's behalf. Nothing goes
    to any spec file.
    """
    if not to_host or not to_host.strip():
        raise ValueError(
            "record_move needs the host the agent moved TO — an empty destination "
            "would open a residency that answers no question"
        )
    return open_residency(history, host=to_host.strip(), now=now)


def legacy_spec_host_notice(*, spec_host: str | None, db_host: str | None) -> str:
    """One line for the dry run about a spec that still carries ``host:``.

    Returns ``""`` when there is nothing to say. Otherwise it states plainly
    which value is authoritative, because the failure mode being avoided is an
    operator reading ``host: nas-03`` in a file, believing it, and being wrong —
    the field is still THERE, it just stopped meaning anything.
    """
    stale = (spec_host or "").strip()
    if not stale:
        return ""
    if db_host and stale != db_host:
        return (
            f"the spec still carries host: {stale}, which is IGNORED — the state db "
            f"says {db_host} and the db is authoritative. Delete the field from the "
            "spec; it is an observation sitting in a file of declarations"
        )
    if db_host:
        return (
            f"the spec still carries host: {stale}; it is ignored (the db agrees, at "
            f"{db_host}). Delete the field — it will not be consulted again"
        )
    return (
        f"the spec carries host: {stale} and the state db knows nothing yet, so this "
        "value will SEED the db once and then be ignored"
    )
