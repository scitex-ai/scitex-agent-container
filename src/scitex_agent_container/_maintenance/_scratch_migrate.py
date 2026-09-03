"""Move ``overlays/<agent>/upper/uvwork`` onto the host scratch volume.

The migration half of ADR-0024. Once ``/uvwork`` is bound from
``<scratch_root>/sac/agents/<agent>/uvwork`` (:mod:`..runtimes._apptainer_scratch`),
the copy that accumulated in each agent's overlay upper is dead weight on the
root LV: shadowed by the bind, never read again, and — measured on
scitex-compute-04 on 2026-09-03 — 11.7 GB for sac, 3.3 GB for scitex-dev,
3.0 GB for scitex-hub, 2.5 GB for scitex-cards, 1.9 GB for scitex-storage.
Moving it across (rather than deleting it) means the next start finds uv and
the venv already in place and skips the rebuild.

The plan is a VALUE built without writing anything (:func:`plan_scratch_migration`);
applying it (:func:`apply_scratch_migration`) does, per agent: copy the tree,
verify every path and byte count against the source, and only then remove the
overlay copy. A verification mismatch keeps the source and says so.

What the plan refuses, and names:

* a RUNNING agent — its container has the overlay mounted; pulling the tree
  out from under it is not a migration. Stop it first.
* an agent whose liveness could not be determined — "unknown" is not
  "stopped". That includes running this verb from somewhere the probe cannot
  see host processes; see :mod:`._scratch_migrate_liveness`.
* a destination already populated — the agent has started under the new
  bind and rebuilt there; the overlay copy is now the older of the two, and
  overwriting fresh with old is not what "migrate" means.
* an overlay TWO agents declare — moving it into one agent's private scratch
  directory takes it away from the other (:func:`_refuse_shared_sources`).
* an image (loopback) overlay — its upper is inside an ext3 image the host
  cannot walk.

The destination is NOT computed here. It comes from
:func:`..runtimes._apptainer_scratch.scratch_uvwork_dir_for`, the same call the
launch bind makes, so "where the migration puts it" and "where the launch
mounts from" cannot be two answers. They were: this module keyed on the spec
DIRECTORY name while the bind keyed on the effective ``config.name``, which
differ for every ``hosts:`` spec (``-<hostname>`` suffix).

A spec that cannot be loaded is ``unreadable`` and makes the plan unsafe, the
same distinction :mod:`._layers_migration_model` draws: a refusal is a spec the
sweep looked at and declined; unreadable means the plan does not describe the
sweep.

The two instruments live beside this module, one responsibility each:
:mod:`._scratch_migrate_measure` (``tree_size`` / ``verify_copy``) and
:mod:`._scratch_migrate_liveness` (``agent_liveness`` / ``liveness_vantage``).
Both are re-exported here, so this module stays the single import site.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .._state.host_scratch import ScratchRoot
from ..runtimes._apptainer_scratch import UVWORK_DIR_MODE, scratch_uvwork_dir_for
from ._roster_state import RosterState, inspect_roster
from ._scratch_migrate_liveness import (  # noqa: F401
    CONTAINER_MARKER_ENV,
    agent_liveness,
    liveness_vantage,
)
from ._scratch_migrate_measure import tree_size, verify_copy  # noqa: F401

#: What the plan decides per agent.
ACTIONS = ("move", "nothing", "refuse")

#: Where the overlay copy sits, relative to the overlay root.
OVERLAY_UVWORK_RELPATH = ("upper", "uvwork")


@dataclass(frozen=True)
class UvworkRow:
    """One agent's overlay ``uvwork`` and what the sweep would do with it."""

    agent: str
    spec_path: Path
    source: Path | None
    dest: Path | None
    bytes: int
    files: int
    running: bool | None
    action: str
    reason: str

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {self.action!r}")

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "spec_path": str(self.spec_path),
            "source": None if self.source is None else str(self.source),
            "dest": None if self.dest is None else str(self.dest),
            "bytes": self.bytes,
            "files": self.files,
            "running": self.running,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MoveResult:
    """What :func:`move_uvwork` did for one row."""

    agent: str
    source: Path
    dest: Path
    bytes: int
    files: int
    moved: bool
    detail: str

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "source": str(self.source),
            "dest": str(self.dest),
            "bytes": self.bytes,
            "files": self.files,
            "moved": self.moved,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScratchPlan:
    """Every agent the sweep looked at, plus the roster it looked in."""

    rows: tuple[UvworkRow, ...]
    roster: RosterState
    scratch: ScratchRoot
    unknown: tuple[str, ...]
    unreadable: tuple[str, ...]

    @property
    def movable(self) -> tuple[UvworkRow, ...]:
        return tuple(r for r in self.rows if r.action == "move")

    @property
    def refused(self) -> tuple[UvworkRow, ...]:
        return tuple(r for r in self.rows if r.action == "refuse")

    @property
    def total_bytes(self) -> int:
        return sum(r.bytes for r in self.movable)

    @property
    def safe_to_apply(self) -> bool:
        """The plan describes the sweep: populated roster, every spec read,
        every ``--agent`` name found."""
        return self.roster.is_populated and not self.unreadable and not self.unknown

    def summary(self) -> str:
        return (
            f"{len(self.movable)} agent(s) to move, {self.total_bytes} bytes; "
            f"{len(self.refused)} refused; "
            f"{sum(1 for r in self.rows if r.action == 'nothing')} with nothing to move"
        )


# ---------------------------------------------------------------------------
# Planning — reads only
# ---------------------------------------------------------------------------


def _overlay_uvwork(config) -> tuple[Path | None, str]:
    """``(overlay upper uvwork, reason-if-None)`` for ``config``."""
    from ..runtimes._apptainer_overlay import (
        is_image_overlay,
        resolve_overlay_declaration,
    )

    root = resolve_overlay_declaration(config)
    if root is None:
        return None, "spec declares no overlay"
    ap = getattr(config, "apptainer", None)
    size = (getattr(ap, "overlay_size", "") or "") if ap is not None else ""
    if is_image_overlay(root, size):
        return None, f"image (loopback) overlay {root}; its upper cannot be walked from the host"
    return root.joinpath(*OVERLAY_UVWORK_RELPATH), ""


def _row_for(
    agent: str, spec_path: Path, config, scratch_root: Path, liveness
) -> UvworkRow:
    source, why = _overlay_uvwork(config)
    # The destination is derived from the CONFIG, through the same function the
    # launch bind calls — never re-spelled from ``agent`` (the spec DIRECTORY
    # name). The two are the same string for a ``host:`` spec and DIFFERENT for
    # a ``hosts:`` one, whose effective id carries a ``-<hostname>`` suffix, so
    # keying the destination on the directory name moved every multi-instance
    # agent's tree to a path no launch would ever mount: copy verified, overlay
    # original deleted, "migrated" reported, and the next start rebuilding uv
    # and the venv from scratch anyway. See ``_apptainer_scratch``'s docstring.
    dest = scratch_uvwork_dir_for(scratch_root, config)
    if source is None:
        return UvworkRow(agent, spec_path, None, dest, 0, 0, None, "nothing", why)
    if not source.is_dir():
        return UvworkRow(
            agent, spec_path, source, dest, 0, 0, None, "nothing",
            f"no uvwork in the overlay upper ({source} is absent)",
        )
    size, files = tree_size(source)
    running, detail = liveness(config)
    if running is None:
        return UvworkRow(
            agent, spec_path, source, dest, size, files, None, "refuse",
            f"liveness unknown ({detail}); only a provably STOPPED agent is moved",
        )
    if running:
        return UvworkRow(
            agent, spec_path, source, dest, size, files, True, "refuse",
            f"RUNNING ({detail}); stop it first: sac agents stop {agent}",
        )
    if dest.is_dir() and any(dest.iterdir()):
        return UvworkRow(
            agent, spec_path, source, dest, size, files, False, "refuse",
            f"destination {dest} is already populated — the agent has rebuilt "
            f"/uvwork on scratch under the new bind, so the overlay copy is "
            f"the older of the two; remove {source} by hand once you agree",
        )
    return UvworkRow(
        agent, spec_path, source, dest, size, files, False, "move",
        f"stopped; {size} bytes in {files} file(s) move to {dest}",
    )


def _refuse_shared_sources(
    rows: Sequence[UvworkRow], owners: dict[Path, list[str]]
) -> tuple[UvworkRow, ...]:
    """Turn every ``move`` whose source another agent also declares into a refusal.

    MEASURED on the live fleet 2026-09-03: ``scitex-hub`` and
    ``scitex-hub-mobile-ux`` declare the SAME ``--overlay`` directory, as do
    ``scitex-cards`` / ``scitex-todo`` and eight ``handyman-*`` specs sharing
    ``local-coder``. The plan listed one 2.6 GiB tree as movable TWICE —
    identical byte counts on two rows. Applying that would move the tree for
    the first agent and then hand the second a source that no longer exists
    (``copytree`` raising mid-sweep), and would file a tree two agents read
    into ONE agent's private per-agent directory, taking it from the other.

    sac cannot pick a winner: which agent owns a shared overlay is a fleet
    decision. So both rows are refused, each naming the others, and the
    operator settles it in the specs.
    """
    out: list[UvworkRow] = []
    for row in rows:
        others = (
            [a for a in owners.get(row.source, ()) if a != row.agent]
            if row.source is not None
            else []
        )
        if row.action == "move" and others:
            out.append(
                replace(
                    row,
                    action="refuse",
                    reason=(
                        f"{row.source} is ALSO declared by "
                        f"{', '.join(sorted(others))}; moving it into "
                        f"{row.dest} would take it away from them. Give each "
                        f"agent its own overlay, or move this tree by hand."
                    ),
                )
            )
        else:
            out.append(row)
    return tuple(out)


def plan_scratch_migration(
    scratch: ScratchRoot,
    *,
    agents_root: Path | None = None,
    only: Sequence[str] = (),
    liveness: Callable = agent_liveness,
) -> ScratchPlan:
    """Build the plan over the fleet roster (or the ``only`` subset). NO writes.

    ``scratch`` must carry a root: a host that decided ``scratch_root: none``
    has nowhere to migrate to, and the CLI refuses before reaching here.

    EVERY spec is loaded, including the ones ``only`` filters out, because
    :func:`_refuse_shared_sources` needs the whole ownership map: two specs
    can name one overlay, and a narrow ``--agent`` run must still be told.
    Only SELECTED agents get a row, a tree walk or a liveness probe, and only
    a SELECTED spec that will not load makes the plan unsafe.
    """
    if scratch.root is None:
        raise ValueError(
            f"plan_scratch_migration needs a scratch root; this host resolved "
            f"source='none' ({scratch.reason})"
        )
    from .._reconcile._pass import fleet_agents_dir, fleet_spec_paths
    from ..config import load_config

    root = agents_root if agents_root is not None else fleet_agents_dir()
    spec_paths = fleet_spec_paths(root)
    roster = inspect_roster(root, spec_paths)
    wanted = set(only)
    seen: set[str] = set()
    rows: list[UvworkRow] = []
    unreadable: list[str] = []
    owners: dict[Path, list[str]] = {}
    for spec_path in spec_paths:
        agent = spec_path.parent.name
        selected = not wanted or agent in wanted
        if selected:
            seen.add(agent)
        try:
            config = load_config(str(spec_path))
        except Exception as exc:  # stx-allow: fallback (reason: one unreadable spec must not abort the sweep; a SELECTED one is recorded and makes the plan unsafe, an unselected one simply stakes no ownership claim)
            if selected:
                unreadable.append(
                    f"{agent}: {type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
                )
            continue
        source, _why = _overlay_uvwork(config)
        if source is not None:
            owners.setdefault(source, []).append(agent)
        if selected:
            rows.append(_row_for(agent, spec_path, config, scratch.root, liveness))
    unknown = tuple(sorted(wanted - seen))
    return ScratchPlan(
        rows=_refuse_shared_sources(rows, owners),
        roster=roster,
        scratch=scratch,
        unknown=unknown,
        unreadable=tuple(unreadable),
    )


# ---------------------------------------------------------------------------
# Applying — copy, verify, then remove the overlay copy
# ---------------------------------------------------------------------------


def move_uvwork(row: UvworkRow) -> MoveResult:
    """Copy ``row.source`` to ``row.dest``, verify, remove the source.

    The source is removed ONLY after :func:`verify_copy` finds no
    difference. On any failure the overlay copy stays and ``detail`` names
    what went wrong; a partial destination is left for inspection rather
    than deleted (it is on scratch, where space is not the problem).
    """
    assert row.action == "move" and row.source is not None and row.dest is not None
    source, dest = row.source, row.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    except shutil.Error as exc:  # stx-allow: fallback (reason: copytree aggregates per-file errors; the source is KEPT, and the count plus the first three failing paths go into this MoveResult.detail — printed by cli_pkg/_agents_scratch_migrate._render_apply and carried in the --json payload's results[]/failed[])
        errs = getattr(exc, "args", [[]])[0]
        first = "; ".join(f"{s} -> {d}: {why}" for s, d, why in list(errs)[:3])
        return MoveResult(
            row.agent, source, dest, row.bytes, row.files, False,
            f"copy failed on {len(errs)} path(s); overlay copy KEPT: {first}",
        )
    os.chmod(dest, UVWORK_DIR_MODE)
    problems = verify_copy(source, dest)
    if problems:
        head = "; ".join(problems[:3])
        return MoveResult(
            row.agent, source, dest, row.bytes, row.files, False,
            f"verification found {len(problems)} difference(s); overlay copy KEPT: {head}",
        )
    shutil.rmtree(source)
    return MoveResult(
        row.agent, source, dest, row.bytes, row.files, True,
        f"moved {row.bytes} bytes in {row.files} file(s); overlay copy removed",
    )


def apply_scratch_migration(plan: ScratchPlan) -> list[MoveResult]:
    """Move every ``move`` row of ``plan``, in plan order."""
    return [move_uvwork(row) for row in plan.movable]


def total_bytes(rows: Iterable[UvworkRow]) -> int:
    return sum(r.bytes for r in rows)


__all__ = [
    "ACTIONS",
    "CONTAINER_MARKER_ENV",
    "OVERLAY_UVWORK_RELPATH",
    "MoveResult",
    "ScratchPlan",
    "UvworkRow",
    "agent_liveness",
    "apply_scratch_migration",
    "liveness_vantage",
    "move_uvwork",
    "plan_scratch_migration",
    "total_bytes",
    "tree_size",
    "verify_copy",
]
