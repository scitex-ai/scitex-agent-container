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

NO FILTER MAY SILENTLY DROP A SPEC. ``--host`` reads each spec to decide,
and a spec it cannot read is KEPT rather than excluded — an unreadable spec
that vanishes from the selection is the "118 done over a fleet of 119"
failure, and it would make the batching flag the thing that disarms the
guard against an unsafe apply.

The writing half — the archive, the atomic write, the measured gate and the
rollback — lives in :mod:`._engines_apply`.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config._engines_line import REFUSED_ALREADY_DECLARED, migrate_engines_block
from ._engines_apply import ApplyResult, apply_engines_migration
from ._roster_state import inspect_roster

__all__ = [
    "STATE_ALREADY",
    "STATE_HELD_BACK",
    "STATE_MIGRATED",
    "STATE_REFUSED",
    "STATE_UNREADABLE",
    "ApplyResult",
    "EnginesPlan",
    "SpecOutcome",
    "apply_engines_migration",
    "plan_engines_migration",
    "read_spec_text",
    "select_spec_paths",
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

    @property
    def is_complete(self) -> bool:
        """Is there NOTHING left for a further run of this sweep to do?

        The only question a scheduled runner actually wants answered, and it
        is not ``exit 0`` and not ``applied``. A run that wrote nothing
        because everything was refused, or because ``--limit`` held the rest
        back, is a run that did its job and left the migration unfinished.
        """
        if self.roster is not None and not self.roster.is_populated:
            return False
        return not (self.migrated or self.held_back or self.refused or self.unreadable)

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
        return "; ".join(parts)


def read_spec_text(path: Path) -> str:
    """A spec's text with its LINE ENDINGS INTACT.

    ``Path.read_text`` opens in universal-newline mode, which silently turns
    every ``\\r\\n`` into ``\\n`` before the caller sees a byte. A CRLF spec
    read that way and written back is rewritten END TO END — the
    unreviewable whole-file diff the operator asked this sweep to avoid —
    and ``_yaml_line_edit.split_ending``'s CRLF handling becomes unreachable
    because the ``\\r`` is already gone. ``newline=""`` is what makes that
    machinery real rather than decorative.
    """
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _spec_hosts(path: Path) -> "set[str] | None":
    """Every host a spec places itself on.

    ``set()`` means the spec was read and places itself nowhere. ``None``
    means it COULD NOT BE READ — a different answer, and the one the host
    filter must not confuse with "no match": an unreadable spec that vanishes
    from the selection is the "118 done over a fleet of 119" failure, and
    ``--host`` would then be the flag that disables the guard blocking an
    unsafe apply.
    """
    try:
        doc = yaml.safe_load(read_spec_text(path))
    except (
        OSError,
        UnicodeDecodeError,
        yaml.YAMLError,
    ):  # stx-allow: fallback (reason: an unreadable spec must reach the PLAN as unreadable, not vanish from the selection filter)
        return None
    spec = (doc or {}).get("spec") or {}
    hosts = {str(spec.get("host"))} if spec.get("host") else set()
    declared = spec.get("hosts")
    if isinstance(declared, list):
        hosts |= {str(h) for h in declared if h}
    return hosts


def _on_a_wanted_host(path: Path, wanted: "set[str]") -> bool:
    """Keep a spec the host filter cannot rule out, so the PLAN reports it."""
    hosts = _spec_hosts(path)
    if hosts is None:
        return True
    return bool(hosts & wanted)


def select_spec_paths(
    root: Path,
    *,
    hosts: "tuple[str, ...]" = (),
    agents: "tuple[str, ...]" = (),
    templates: bool = False,
) -> "tuple[list[Path], list[str]]":
    """Which specs THIS run touches, plus the template names it left out.

    Batching is the operator's own condition for trusting a 119-file rewrite:
    ``agents`` names an explicit set and ``hosts`` takes one machine at a
    time. The order is sorted so a dry-run and the apply that follows it
    agree on what they are looking at.

    THE BATCH SIZE IS NOT HERE. ``--limit`` caps what gets WRITTEN, and
    which specs those are is only knowable after planning — see
    :func:`plan_engines_migration`. Capping the glob instead re-selected the
    same first N on every run, so the second batch wrote nothing and
    reported the sweep complete.
    """
    every = sorted(Path(root).glob("*/spec.yaml"))
    skipped = [p.parent.name for p in every if p.parent.name.startswith("_")]
    picked = [p for p in every if templates or not p.parent.name.startswith("_")]
    if agents:
        wanted = set(agents)
        picked = [p for p in picked if p.parent.name in wanted]
    if hosts:
        wanted_hosts = set(hosts)
        picked = [p for p in picked if _on_a_wanted_host(p, wanted_hosts)]
    return picked, ([] if templates else skipped)


def plan_spec(path: Path) -> SpecOutcome:
    """Plan ONE spec. Reads it; writes nothing."""
    agent = path.parent.name
    try:
        before = read_spec_text(path)
    except (
        OSError,
        UnicodeDecodeError,
    ) as exc:  # stx-allow: fallback (reason: one unreadable spec must not abort a 119-spec sweep; it is recorded and makes the plan unsafe)
        return SpecOutcome(agent, path, STATE_UNREADABLE, detail=str(exc))

    edit = migrate_engines_block(before, path=str(path))
    if not edit.changed:
        state = (
            STATE_ALREADY if edit.reason == REFUSED_ALREADY_DECLARED else STATE_REFUSED
        )
        return SpecOutcome(
            agent, path, state, reason=edit.reason or "", detail=edit.detail
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
                    )
                )
                continue
            budget -= 1
        capped.append(outcome)
    return tuple(capped)


def plan_engines_migration(
    spec_paths: "list[Path]",
    *,
    root: "Path | None" = None,
    skipped_templates: "list[str]" = (),
    limit: "int | None" = None,
) -> EnginesPlan:
    """What the sweep WOULD do over ``spec_paths``. Reads only."""
    if limit is not None and limit < 1:
        raise ValueError(
            f"limit must be a positive count of specs to write; got {limit!r}. "
            "A Python slice accepts a negative bound and would silently drop "
            "the LAST spec instead of taking the first one."
        )
    paths = list(spec_paths)
    return EnginesPlan(
        outcomes=_cap_batch(tuple(plan_spec(p) for p in paths), limit),
        roster=inspect_roster(root, paths),
        skipped_templates=tuple(skipped_templates),
    )
