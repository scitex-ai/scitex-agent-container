"""Plan the ``spec.engines`` sweep — five outcomes, never two.

The ``to_home_layers`` kit next door plans a ONE-LINE insert, so its
:class:`.._layers_migration_model.MigrationPlan` calls any edit touching other
than exactly one line MALFORMED. This sweep writes a block, so it needs its own
plan rather than a loosened version of that one — loosening the check would
disarm it for the migration it was written for.

WHAT IT KEEPS APART. Every spec lands in exactly one bucket and each is
reported:

  ``migrated``          the edit is ready, with its diff.
  ``already-migrated``  the spec declares ``spec.engines`` already. Finished,
                        not refused: a second run over a swept fleet reports
                        119 of these and writes nothing.
  ``refused``           the editor looked and declined, NAMING why. Expected,
                        counted, and NOT an error exit — a human resolves it.
  ``unreadable``        the file could not be read at all. This never reached
                        the editor, so the plan does not describe the sweep;
                        it makes the plan unsafe.
  ``held-back``         migratable, and past this run's ``--limit``. Counted
                        and named, because a batch that does not say what it
                        deferred reads exactly like a finished sweep.

NO FILTER MAY SILENTLY DROP A SPEC, and nothing else may either. Which specs
this run looks at — and everything it declined to look at, from a shadowed
second copy to a ``--agent`` value that matched nothing — is
:mod:`._engines_selection`'s job.

WHAT NARROWS THE RUN NARROWS THE CLAIM. Whatever the selection left out is
carried onto the plan and consumed by :attr:`EnginesPlan.outstanding`, so the
one boolean a scheduled runner reads cannot disagree with the prose a human
reads. That disagreement is the defect this module keeps re-learning: the
templates and the ``--agent`` filter were both printed and both invisible to
``migration_complete``.

THE VERSION FLOOR IS NOT OPTIONAL HERE. :func:`plan_engines_migration` REQUIRES
a ``floor``; it used to default to None, and the documented public planner then
planned a spec pinned on a pre-engines host as migrated and ``safe_to_apply``.
The guard belonged in the data path rather than in the one caller that
remembered to pass it. ``EngineFloor.disabled()`` is how a caller says "no
floor" out loud.

The writing half — the archive, the atomic write, the measured gate and the
rollback — lives in :mod:`._engines_apply`.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from ..config._engines_line import REFUSED_ALREADY_DECLARED, migrate_engines_block
from ._engines_apply import ApplyResult, apply_engines_migration
from ._engines_floor import REFUSED_HOST_PREDATES_ENGINES, EngineFloor
from ._engines_selection import (
    ShadowedSpec,
    SpecSelection,
    read_spec_text,
    select_spec_paths,
    select_spec_paths_over_roots,
    spec_hosts_from_text,
)
from ._roster_state import inspect_roster, inspect_roster_over_roots

__all__ = [
    "STATE_ALREADY",
    "STATE_HELD_BACK",
    "STATE_MIGRATED",
    "STATE_REFUSED",
    "STATE_UNREADABLE",
    "ApplyResult",
    "EnginesPlan",
    "ShadowedSpec",
    "SpecOutcome",
    "SpecSelection",
    "apply_engines_migration",
    "plan_engines_migration",
    "read_spec_text",
    "select_spec_paths",
    "select_spec_paths_over_roots",
]

STATE_MIGRATED = "migrated"
STATE_ALREADY = "already-migrated"
STATE_REFUSED = "refused"
STATE_UNREADABLE = "unreadable"
#: Migratable, but beyond this run's ``--limit``. A fifth bucket rather than
#: a silent drop: a batch that does not say what it held back is a batch a
#: scheduled runner mistakes for a finished sweep.
STATE_HELD_BACK = "held-back"


@dataclass(frozen=True)
class SpecOutcome:
    """What the sweep would do to ONE spec, and why."""

    agent: str
    path: Path
    state: str
    reason: str = ""
    detail: str = ""
    engine_keys: "tuple[str, ...]" = ()
    default_key: str = ""
    new_text: "str | None" = None
    diff: str = ""
    #: Every host THIS spec's text places itself on, sorted. ``None`` means
    #: they could not be read — kept apart from ``()`` ("it names none") for
    #: the reason ``_engines_selection.spec_hosts`` states, and carried on the
    #: outcome so the report can roll up the floor's basis without re-reading
    #: the file and risking two answers about one spec.
    hosts: "tuple[str, ...] | None" = None

    @property
    def will_write(self) -> bool:
        return self.state == STATE_MIGRATED and self.new_text is not None


@dataclass(frozen=True)
class EnginesPlan:
    """What the sweep would do, in full, before it does any of it."""

    outcomes: "tuple[SpecOutcome, ...]" = ()
    roster: object = None
    #: Template specs (``_``-prefixed dirs) deliberately left out of the
    #: selection. Named rather than dropped: ``sac agents create`` copies
    #: these, so an unmigrated template re-introduces the legacy shape on
    #: every agent created after the sweep.
    skipped_templates: "tuple[str, ...]" = field(default=())
    #: Spec files a LATER root held for an agent an earlier root already
    #: supplied. Exactly one copy is written; the other is a real file this
    #: run never looked at, and no later run can reach it either.
    shadowed: "tuple[ShadowedSpec, ...]" = field(default=())
    #: The narrowing selectors this run was given, spelled as the flags that
    #: produced them (``--agent business``). Non-empty means the census
    #: describes a SUBSET of the roster, which is why it costs completeness.
    selectors: "tuple[str, ...]" = field(default=())
    #: ``--agent`` values that matched no spec under any root.
    unmatched_agents: "tuple[str, ...]" = field(default=())
    #: ``--host`` values no selected spec places itself on.
    unmatched_hosts: "tuple[str, ...]" = field(default=())

    def _of(self, state: str) -> "tuple[SpecOutcome, ...]":
        return tuple(o for o in self.outcomes if o.state == state)

    @property
    def migrated(self) -> "tuple[SpecOutcome, ...]":
        return self._of(STATE_MIGRATED)

    @property
    def already(self) -> "tuple[SpecOutcome, ...]":
        return self._of(STATE_ALREADY)

    @property
    def refused(self) -> "tuple[SpecOutcome, ...]":
        return self._of(STATE_REFUSED)

    @property
    def unreadable(self) -> "tuple[SpecOutcome, ...]":
        return self._of(STATE_UNREADABLE)

    @property
    def held_back(self) -> "tuple[SpecOutcome, ...]":
        return self._of(STATE_HELD_BACK)

    def remaining(self, *, migrated_written: bool = False) -> "tuple[str, ...]":
        """Everything a further run still has to do, each named in one line.

        :attr:`is_complete` is exactly "this is empty", so the boolean the
        machine reads and the prose the human reads cannot drift apart —
        which they did: the skipped templates and the ``--agent`` filter were
        both printed to a human and both invisible to the boolean.

        ``migrated_written`` is the ONE difference between the question
        before a write and the question after a successful one: a completed
        apply retires the ``migrated`` bucket and nothing else. The caller
        that answers the second question passes this flag rather than
        re-listing the conditions, because re-listing them is how the two
        answers drifted apart in the first place.
        """
        left: "list[str]" = []
        if self.roster is not None and not self.roster.is_populated:
            left.append(self.roster.describe())
        if self.selectors:
            left.append(
                f"this run was NARROWED to {', '.join(self.selectors)} — the "
                f"rest of the roster was never examined"
            )
        if self.skipped_templates:
            left.append(
                f"{len(self.skipped_templates)} template spec(s) not migrated "
                f"({', '.join(self.skipped_templates)}) — `sac agents create` "
                f"copies them, so every agent minted after this sweep "
                f"re-introduces the legacy shape"
            )
        if self.shadowed:
            left.append(
                f"{len(self.shadowed)} spec file(s) shadowed by an earlier root "
                f"({', '.join(s.agent for s in self.shadowed)}) — still on disk, "
                f"still legacy, and no run of this sweep can reach them"
            )
        if self.migrated and not migrated_written:
            left.append(f"{len(self.migrated)} spec(s) still to write")
        if self.held_back:
            left.append(f"{len(self.held_back)} spec(s) held back by --limit")
        if self.refused:
            left.append(f"{len(self.refused)} spec(s) REFUSED")
        if self.unreadable:
            left.append(f"{len(self.unreadable)} spec(s) unreadable")
        return tuple(left)

    @property
    def outstanding(self) -> "tuple[str, ...]":
        """:meth:`remaining` for the question BEFORE any write."""
        return self.remaining()

    @property
    def is_complete(self) -> bool:
        """Is there NOTHING left for a further run of this sweep to do?

        The only question a scheduled runner actually wants answered, and it
        is not ``exit 0`` and not ``applied``. A run that wrote nothing
        because everything was refused, or because ``--limit`` held the rest
        back, is a run that did its job and left the migration unfinished.

        A NARROWED run is likewise not a finished migration, and that is the
        exact shape this field exists to stop: ``-a business --apply`` over a
        113-spec root printed "this is what a completed one looks like",
        named the FULL root it had not covered, and reported
        ``migration_complete: true``. The templates and a shadowed second copy
        are the same failure in two other costumes — work left behind that the
        prose already reported and the boolean did not.
        """
        return not self.outstanding

    @property
    def safe_to_apply(self) -> bool:
        """Refusals do NOT make a plan unsafe. An unsearched roster does.

        A named refusal is a legitimate outcome a human resolves. A spec that
        could not be read, or a roster that was never searched, both mean the
        plan does not describe the sweep — see :mod:`._roster_state` for the
        measured incident behind the second one.
        """
        if self.roster is not None and not self.roster.is_populated:
            return False
        return not self.unreadable

    def summary(self) -> str:
        if self.roster is not None and not self.roster.is_populated:
            return self.roster.describe()
        parts = [
            f"{len(self.migrated)} would be migrated",
            f"{len(self.already)} already migrated",
        ]
        if self.held_back:
            parts.append(
                f"{len(self.held_back)} held back by --limit — run again to "
                f"take the next batch"
            )
        if self.refused:
            names = ", ".join(sorted(o.agent for o in self.refused))
            parts.append(f"{len(self.refused)} REFUSED ({names})")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable — do not apply")
        if self.selectors:
            parts.append(
                f"NARROWED by {', '.join(self.selectors)} — not a census of "
                f"the roster"
            )
        if self.shadowed:
            parts.append(f"{len(self.shadowed)} shadowed by an earlier root")
        return "; ".join(parts)


def plan_spec(path: Path, *, floor: EngineFloor) -> SpecOutcome:
    """Plan ONE spec. Reads it; writes nothing.

    ``floor`` is the VERSION FLOOR (:mod:`._engines_floor`) and is REQUIRED —
    pass ``EngineFloor.disabled()`` to plan without one. It is consulted only
    where it can change the outcome: where a block WOULD be written, and
    where one is already declared on a host that cannot parse it. A spec the
    editor refuses for its own reason gets no block either way, so the
    editor's reason is the actionable one and is left in place.
    """
    agent = path.parent.name
    try:
        before = read_spec_text(path)
    except (
        OSError,
        UnicodeDecodeError,
    ) as exc:  # stx-allow: fallback (reason: one unreadable spec must not abort a 119-spec sweep; it is recorded and makes the plan unsafe)
        return SpecOutcome(agent, path, STATE_UNREADABLE, detail=str(exc))

    hosts = spec_hosts_from_text(before)
    declared = None if hosts is None else tuple(sorted(hosts))
    edit = migrate_engines_block(before, path=str(path))
    blocked = _floor_refusal(agent, path, hosts, edit, floor)
    if blocked is not None:
        return blocked
    if not edit.changed:
        state = (
            STATE_ALREADY if edit.reason == REFUSED_ALREADY_DECLARED else STATE_REFUSED
        )
        return SpecOutcome(
            agent,
            path,
            state,
            reason=edit.reason or "",
            detail=edit.detail,
            hosts=declared,
        )
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            edit.text.splitlines(keepends=True),
            fromfile=f"a/{agent}/spec.yaml",
            tofile=f"b/{agent}/spec.yaml",
        )
    )
    return SpecOutcome(
        agent,
        path,
        STATE_MIGRATED,
        engine_keys=edit.engine_keys,
        default_key=edit.default_key,
        new_text=edit.text,
        diff=diff,
        hosts=declared,
    )


#: Appended when the floor blocks a spec that ALREADY declares engines ON A
#: HOST MEASURED AS PRE-ENGINES. That is not a migration this sweep would make
#: — it is a spec which cannot load on its own host TODAY, and reporting it as
#: "already migrated" would file a live incident under the one bucket that
#: means "finished".
#:
#: ONLY that branch. It used to be appended to every floor refusal of an
#: already-declaring spec, so the not-measured and no-host refusals asserted as
#: fact the very thing their own preceding sentence says nobody knows: "absent
#: from the measured roster … This spec ALREADY declares spec.engines, so it
#: does not load on that host today." Nobody measured that host; nothing had
#: established any load failure on it. And under the no-host reason there is no
#: "that host" to refer to at all.
_ALREADY_ON_AN_INCAPABLE_HOST = (
    " This spec ALREADY declares spec.engines, so it does not load on that "
    "host today — nothing here writes it, and a human has to resolve it."
)


def _floor_refusal(
    agent: str,
    path: Path,
    hosts: "set[str] | None",
    edit,
    floor: EngineFloor,
) -> "SpecOutcome | None":
    """The floor's refusal for this spec, or None to leave the outcome alone."""
    already = edit.reason == REFUSED_ALREADY_DECLARED
    if not edit.changed and not already:
        return None
    verdict = floor.verdict_for(hosts)
    if not verdict.blocks:
        return None
    detail = verdict.detail
    if already and verdict.reason == REFUSED_HOST_PREDATES_ENGINES:
        detail += _ALREADY_ON_AN_INCAPABLE_HOST
    return SpecOutcome(
        agent,
        path,
        STATE_REFUSED,
        reason=verdict.reason,
        detail=detail,
        hosts=None if hosts is None else tuple(sorted(hosts)),
    )


def _cap_batch(
    outcomes: "tuple[SpecOutcome, ...]", limit: "int | None"
) -> "tuple[SpecOutcome, ...]":
    """Hold back every migratable outcome past the ``limit``-th one.

    THE CAP IS ON WHAT IS WRITTEN, not on what is looked at, and that is the
    whole difference between a batch flag that advances and one that cannot.
    Already-migrated, refused and unreadable specs do not consume the budget:
    they cost no write, and letting them consume it is how ``--limit 2``
    stalls forever on two permanently-refused specs.
    """
    if limit is None:
        return outcomes
    budget = limit
    capped: list[SpecOutcome] = []
    for outcome in outcomes:
        if outcome.state == STATE_MIGRATED:
            if budget <= 0:
                capped.append(
                    SpecOutcome(
                        outcome.agent,
                        outcome.path,
                        STATE_HELD_BACK,
                        reason="held back by --limit",
                        engine_keys=outcome.engine_keys,
                        default_key=outcome.default_key,
                        hosts=outcome.hosts,
                    )
                )
                continue
            budget -= 1
        capped.append(outcome)
    return tuple(capped)


def plan_engines_migration(
    spec_paths: "list[Path]",
    *,
    floor: EngineFloor,
    root: "Path | None" = None,
    roots: "tuple[Path, ...] | None" = None,
    skipped_templates: "list[str]" = (),
    shadowed: "tuple[ShadowedSpec, ...]" = (),
    selectors: "tuple[str, ...]" = (),
    unmatched_agents: "tuple[str, ...]" = (),
    unmatched_hosts: "tuple[str, ...]" = (),
    limit: "int | None" = None,
) -> EnginesPlan:
    """What the sweep WOULD do over ``spec_paths``. Reads only.

    ``roots`` is the several-roots form of ``root`` and takes precedence when
    given: the default sweep searches every user-scope root rather than one,
    and a roster that named only the first of them would be a report about a
    directory the sweep did not confine itself to.

    ``floor`` is the version floor and is REQUIRED. Omitting it used to
    disable it, so the documented public entry point — called the documented
    way, ``plan_engines_migration(paths, root=root)`` — planned a spec pinned
    on a measured pre-engines host as ``migrated`` and ``safe_to_apply``. The
    fleet path was guarded only because the single CLI call site remembered;
    a second entry point inherited nothing and got no error to trip over.
    Pass ``EngineFloor.disabled()`` to plan without a floor on purpose.
    """
    if floor is None:
        raise TypeError(
            "plan_engines_migration requires a floor. Pass "
            "EngineFloor.with_overrides(...) for a real sweep, or "
            "EngineFloor.disabled() to plan WITHOUT the version floor. There "
            "is no default because the default was 'no floor', and a floorless "
            "plan reports a spec pinned on a pre-engines host as safe to write."
        )
    if limit is not None and limit < 1:
        raise ValueError(
            f"limit must be a positive count of specs to write; got {limit!r}. "
            "A Python slice accepts a negative bound and would silently drop "
            "the LAST spec instead of taking the first one."
        )
    paths = list(spec_paths)
    roster = (
        inspect_roster_over_roots(roots, paths)
        if roots is not None
        else inspect_roster(root, paths)
    )
    return EnginesPlan(
        outcomes=_cap_batch(tuple(plan_spec(p, floor=floor) for p in paths), limit),
        roster=roster,
        skipped_templates=tuple(skipped_templates),
        shadowed=tuple(shadowed),
        selectors=tuple(selectors),
        unmatched_agents=tuple(unmatched_agents),
        unmatched_hosts=tuple(unmatched_hosts),
    )
