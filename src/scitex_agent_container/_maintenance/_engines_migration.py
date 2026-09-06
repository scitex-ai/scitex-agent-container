"""Plan and apply the ``spec.engines`` sweep — three outcomes, never two.

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

THE APPLY GATE IS A MEASUREMENT, NOT AN ARGUMENT. The edit restates the
backend a spec already declares, so it cannot change what an agent starts on
— that is the argument, and an argument has never stopped a bulk edit from
being wrong. So the apply loads every selected spec through the production
loader BEFORE writing, writes, loads them all again, and RESTORES EVERY
ORIGINAL unless the effective backend (harness, model, provider endpoint,
pinned account) is identical for every one. A spec it could not load on
either side blocks the sweep instead of being skipped.

Originals are archived before the first byte changes, so the rollback is a
copy-back and not a reconstruction.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config._engines_line import REFUSED_ALREADY_DECLARED, migrate_engines_block
from ._roster_state import inspect_roster

__all__ = [
    "STATE_ALREADY",
    "STATE_MIGRATED",
    "STATE_REFUSED",
    "STATE_UNREADABLE",
    "ApplyResult",
    "EnginesPlan",
    "SpecOutcome",
    "apply_engines_migration",
    "plan_engines_migration",
    "select_spec_paths",
]

STATE_MIGRATED = "migrated"
STATE_ALREADY = "already-migrated"
STATE_REFUSED = "refused"
STATE_UNREADABLE = "unreadable"


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
        if self.refused:
            names = ", ".join(sorted(o.agent for o in self.refused))
            parts.append(f"{len(self.refused)} REFUSED ({names})")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable — do not apply")
        return "; ".join(parts)


def _spec_hosts(path: Path) -> "set[str]":
    """Every host a spec places itself on. Empty when it says nothing."""
    try:
        doc = yaml.safe_load(path.read_text())
    except (
        OSError,
        UnicodeDecodeError,
        yaml.YAMLError,
    ):  # stx-allow: fallback (reason: an unreadable spec must reach the PLAN as unreadable, not vanish from the selection filter)
        return set()
    spec = (doc or {}).get("spec") or {}
    hosts = {str(spec.get("host"))} if spec.get("host") else set()
    declared = spec.get("hosts")
    if isinstance(declared, list):
        hosts |= {str(h) for h in declared if h}
    return hosts


def select_spec_paths(
    root: Path,
    *,
    hosts: "tuple[str, ...]" = (),
    agents: "tuple[str, ...]" = (),
    templates: bool = False,
    limit: "int | None" = None,
) -> "tuple[list[Path], list[str]]":
    """Which specs THIS run touches, plus the template names it left out.

    Batching is the operator's own condition for trusting a 119-file rewrite:
    ``agents`` names an explicit set, ``hosts`` takes one machine at a time,
    and ``limit`` caps the batch whatever the other two selected. The order is
    sorted so a dry-run and the apply that follows it agree on which specs the
    cap kept.
    """
    every = sorted(Path(root).glob("*/spec.yaml"))
    skipped = [p.parent.name for p in every if p.parent.name.startswith("_")]
    picked = [p for p in every if templates or not p.parent.name.startswith("_")]
    if agents:
        wanted = set(agents)
        picked = [p for p in picked if p.parent.name in wanted]
    if hosts:
        wanted_hosts = set(hosts)
        picked = [p for p in picked if _spec_hosts(p) & wanted_hosts]
    if limit is not None:
        picked = picked[:limit]
    return picked, ([] if templates else skipped)


def plan_spec(path: Path) -> SpecOutcome:
    """Plan ONE spec. Reads it; writes nothing."""
    agent = path.parent.name
    try:
        before = path.read_text()
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


def plan_engines_migration(
    spec_paths: "list[Path]",
    *,
    root: "Path | None" = None,
    skipped_templates: "list[str]" = (),
) -> EnginesPlan:
    """What the sweep WOULD do over ``spec_paths``. Reads only."""
    paths = list(spec_paths)
    return EnginesPlan(
        outcomes=tuple(plan_spec(p) for p in paths),
        roster=inspect_roster(root, paths),
        skipped_templates=tuple(skipped_templates),
    )


@dataclass(frozen=True)
class ApplyResult:
    """What the apply actually did."""

    written: "tuple[str, ...]" = ()
    archive_dir: "Path | None" = None
    applied: bool = False
    refused: str = ""
    rolled_back: str = ""
    drift: "tuple[str, ...]" = ()


def _backend_snapshot(path: Path):
    """The effective backend this spec resolves to, through the real loader."""
    from ..config import load_config

    config = load_config(path)
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None)
    endpoint = None
    if provider is not None:
        endpoint = (
            str(getattr(provider, "base_url", "") or ""),
            str(getattr(provider, "auth_token_env", "") or ""),
        )
    return (
        str(getattr(config, "harness", "") or ""),
        str(getattr(claude, "model", "") or ""),
        endpoint,
        str(getattr(claude, "account", "") or ""),
    )


def _snapshot_all(paths):
    """``(snapshots, unmeasurable)`` — a spec that will not load is named."""
    snapshots: dict[str, object] = {}
    unmeasurable: list[str] = []
    for path in paths:
        try:
            snapshots[str(path)] = _backend_snapshot(path)
        except Exception as exc:  # stx-allow: fallback (reason: a spec the loader rejects is UNMEASURABLE, the honest third value; enumerating loader exception types would turn any new one into a crash mid-sweep)
            unmeasurable.append(f"{path.parent.name}: {type(exc).__name__}: {exc}")
    return snapshots, unmeasurable


def apply_engines_migration(plan: EnginesPlan, archive_dir: Path) -> ApplyResult:
    """Archive, write, re-measure, and undo unless the backends are identical."""
    targets = list(plan.migrated)
    if not targets:
        return ApplyResult(applied=True)
    paths = [o.path for o in targets]

    before, unmeasurable = _snapshot_all(paths)
    if unmeasurable:
        # Refuse BEFORE writing. The gate would catch this afterwards too, but
        # writing N files to learn something knowable beforehand is a rollback
        # waiting to be needed, not a safety property.
        return ApplyResult(
            refused=(
                f"{len(unmeasurable)} spec(s) could not be loaded BEFORE the "
                f"sweep, so no post-write comparison could prove them "
                f"unchanged: " + "; ".join(unmeasurable)
            )
        )

    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    for outcome in targets:
        shutil.copy2(outcome.path, archive_dir / f"{outcome.agent}.spec.yaml")
    for outcome in targets:
        outcome.path.write_text(outcome.new_text or "")

    after, still_unmeasurable = _snapshot_all(paths)
    drift = [
        f"{Path(key).parent.name}: {before[key]!r} -> {after[key]!r}"
        for key in before
        if key in after and after[key] != before[key]
    ]
    if still_unmeasurable or drift:
        for outcome in targets:
            shutil.copy2(archive_dir / f"{outcome.agent}.spec.yaml", outcome.path)
        return ApplyResult(
            archive_dir=archive_dir,
            rolled_back=(
                f"{len(drift)} spec(s) changed backend and "
                f"{len(still_unmeasurable)} stopped loading; every original was "
                f"restored from {archive_dir}"
            ),
            drift=tuple(drift + still_unmeasurable),
        )
    return ApplyResult(
        written=tuple(o.agent for o in targets),
        archive_dir=archive_dir,
        applied=True,
    )
