"""SPEC is declared intent. STATE DB is observed reality. Neither may hold the other.

The operator's item #2, and he was explicit that it is a CONSTRAINT rather than
a step in the sequence: 「sac は spec と状態 db を明確に使い分ける！」. A
relocation is where the two are most easily confused, so this module gives each
vocabulary a name and refuses a write that mixes them.

    SPEC       what a human declared. Authored, committed to git, diffable,
               true before anything runs and still true while nothing runs.
               NOTHING MACHINE-WRITTEN EVER LANDS HERE.
    STATE DB   what was observed. Written by machines, never by hand, and
               meaningless without a timestamp.

``host`` IS A STATE FIELD, AND THAT IS THE WHOLE POINT OF THE SPLIT. It was in
the spec for years and it was never intent: where an agent actually runs is an
OBSERVATION, and a human writing it down is guessing about a fact rather than
declaring a preference. The operator settled it on 2026-08-11 —「設定ファイル、
人が書くものはファイル、状態は db」— which is why a relocation writes the host to
the db and writes NOTHING to any spec file. That also removes the last reason a
machine would ever touch a git-tracked, human-authored document that exists in
two copies on two hosts, free to diverge.

A consequence worth stating out loud, because it is what makes this a refusal
rather than a note: ``spec_patch(host=…)`` now RAISES. Any code path that tried
to write a host into a spec fails at the call site with the field named.

THE BUG NOT TO IMITATE, and it is live today. ``sac agents list`` returns
``status`` values ``defined / stopped / unknown / invalid``. "defined" is a SPEC
fact — a file exists — rendered in a STATE column, which is why the listing
reports zero running agents while agents are demonstrably running: the column
answers "is there a spec" and is read as "is there a process". The same listing
reports ``host`` as the literal string ``'local'`` on every row, which is a
placeholder in a field that is supposed to carry an observation. Both are the
failure this module exists to make unrepresentable for relocation's own writes.

WHY A REFUSAL AND NOT A CONVENTION. A comment saying "do not put runtime state
in the spec" is obeyed until the first hurry. :func:`spec_patch` and
:func:`state_record` each know both vocabularies and raise on a field from the
wrong one, so the mistake fails at the call site with the field named, and a
test can pin it. The cost is that a genuinely new field has to be declared here
before it can be written — which is the point, since that is exactly the moment
someone should be asked which of the two it is.

FIELDS NOT IN EITHER SET ARE REFUSED TOO, rather than passed through. A
pass-through default means the next unfamiliar key lands wherever it was sent,
and the separation holds only for the fields someone remembered to list.

Pure: dicts in, dicts out.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "SPEC_FIELDS",
    "STATE_FIELDS",
    "classify_field",
    "spec_patch",
    "state_record",
]

#: DECLARED INTENT. Everything here is authored by a human and lives in the spec
#: file. A relocation writes NONE of them — they are listed so that sending one
#: to the state db is caught rather than accepted.
#:
#: ``host`` is deliberately ABSENT. See the module docstring: it is an
#: observation, it now lives in the state db, and a spec write naming it must
#: fail rather than succeed quietly.
SPEC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "runtime",
        "image",
        "binds",
        "raw_args",
        "env",
        "a2a_port",
        "cardinality",
        "model",
        "repo",
        "workdir",
        "labels",
        "role",
    }
)

#: OBSERVED REALITY. Written by the coordinator from measurements, never by
#: hand. ``migration_retained`` is the item-#9 flag: the fact of a migration
#: stays in the state db after the relocation completes and is never discarded.
STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "agent",
        #: WHERE THE AGENT ACTUALLY RUNS. Moved here from the spec on
        #: 2026-08-11; a relocation's DONE phase is the only thing that writes
        #: it, via the residency record.
        "host",
        "host_seeded_from_spec",
        "from_host",
        "to_host",
        "phase",
        "steps",
        "started_at",
        "finished_at",
        "lease_holder",
        "lease_fence",
        "handshake_proven",
        "handshake_observed_by",
        "source_stopped_verified",
        "standby_left_running",
        "transcript_carried",
        "origin",
        "residency",
        "migration_retained",
        "outcome_code",
        "outcome_reason",
    }
)


def classify_field(name: str) -> str:
    """``"spec"``, ``"state"``, or a raise naming the field.

    Refuses a name in BOTH sets as loudly as a name in neither: a field that
    could legitimately be written to either place is the ambiguity this module
    exists to remove, and silently preferring one would hide it.
    """
    in_spec = name in SPEC_FIELDS
    in_state = name in STATE_FIELDS
    if in_spec and in_state:
        raise ValueError(
            f"{name!r} is declared in BOTH SPEC_FIELDS and STATE_FIELDS — decide which "
            "it is. A field that means declared intent in one place and an observation "
            "in another is how `sac agents list` ends up reporting 'defined' as a status"
        )
    if in_spec:
        return "spec"
    if in_state:
        return "state"
    raise ValueError(
        f"{name!r} is in neither SPEC_FIELDS nor STATE_FIELDS. Declare it in one of "
        "them first — passing unknown fields through means the separation only holds "
        "for the fields someone remembered to list"
    )


def _partition(values: dict[str, Any], *, want: str, other: str) -> dict[str, Any]:
    wrong = sorted(k for k in values if classify_field(k) != want)
    if wrong:
        raise ValueError(
            f"refusing to write {', '.join(repr(k) for k in wrong)} to the {want.upper()}: "
            f"{'that field is' if len(wrong) == 1 else 'those fields are'} {other} data. "
            f"The {want} carries "
            + (
                "what a human declared; observations belong in the state db"
                if want == "spec"
                else "what was observed; declarations belong in the spec file"
            )
        )
    return dict(values)


def spec_patch(**values: Any) -> dict[str, Any]:
    """The subset of ``values`` a spec file may carry, or a raise naming the rest.

    RELOCATION NEVER CALLS THIS, and that is the point rather than an oversight:
    a relocation writes only the state db, so the one field it once wanted here
    (``host``) is now refused. The function exists so the refusal is available to
    every OTHER spec write in sac — the moment one of them reaches for an
    observation, it fails at the call site with the field named instead of
    quietly putting a machine-owned fact into a human-authored file.
    """
    if not values:
        raise ValueError(
            "spec_patch() with no fields writes nothing; call it with the edit"
        )
    return _partition(values, want="spec", other="observed")


def state_record(**values: Any) -> dict[str, Any]:
    """The subset of ``values`` the state db may carry, or a raise naming the rest.

    ``agent``, ``from_host`` and ``to_host`` are required: a relocation row that
    does not say who moved and between which hosts is a row that cannot be
    joined to anything, which is how the cards table ended up with a NULL
    ``host`` on 3247 of 3424 rows.
    """
    record = _partition(values, want="state", other="declared")
    missing = [k for k in ("agent", "from_host", "to_host") if not record.get(k)]
    if missing:
        raise ValueError(
            f"a relocation state row must name {', '.join(missing)} — a row that does "
            "not say who moved and between which hosts cannot be joined to anything "
            "later, which is exactly the attribution gap residency was added to close"
        )
    return record
