"""Apply the ``to_home_layers`` sweep TRANSACTIONALLY, or leave nothing changed.

The sweep edits ~101 hand-maintained ``spec.yaml`` files. Two things can go
wrong that a per-file check cannot see:

* the plan itself is wrong (an edit touches more than the one intended line), and
* the edits are individually fine but change what an agent ARMS.

The second only becomes visible AFTER writing, by re-deriving hook arming from
the migrated specs. So "check, then write" is not available — the honest shape
is write, verify, and UNDO if the verification fails. That is why every
original is archived first: the archive is not a courtesy copy, it is the
rollback path, and without it the post-write gate would have nothing to offer
but a report of damage already done.

Ordering, and why each step is where it is:

  1. refuse unless :attr:`MigrationPlan.safe_to_apply` — never write from a
     plan that does not describe what would happen
  2. archive every target to ``.old/<timestamp>/`` — the rollback path exists
     before the first byte changes
  3. write
  4. re-derive hook arming and diff it against the pre-write snapshot
  5. if the diff is not ``safe``, RESTORE from the archive and report refusal

A partially-applied sweep is the one outcome this must never produce, because
it leaves the fleet in a state no one planned and no one can describe.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ._layers_migration_model import MigrationPlan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationResult:
    """What the apply did. Same shape whether it succeeded, refused, or undid.

    ``refused`` and ``rolled_back`` are distinct on purpose: refusing BEFORE
    writing and undoing AFTER writing leave the filesystem identical but say
    very different things about what was learned, and an operator reading this
    needs to know which happened.
    """

    written: "tuple[str, ...]" = ()
    archive_dir: "Path | None" = None
    #: Set when the apply declined to write at all. None means it wrote.
    refused: "str | None" = None
    #: Set when it wrote, failed verification, and restored the originals.
    rolled_back: "str | None" = None

    @property
    def applied(self) -> bool:
        """True only when edits were written AND survived verification."""
        return bool(self.written) and not self.refused and not self.rolled_back


def archive_originals(plan: MigrationPlan, archive_dir: Path) -> "list[Path]":
    """Copy every spec the plan would write into ``archive_dir``.

    Flat, keyed by agent name, because two agents' specs share the basename
    ``spec.yaml`` and a flat copy of both would silently keep only one — an
    archive that loses half its entries is worse than none, since it invites a
    rollback that cannot complete.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for edit in plan.writable:
        dest = archive_dir / f"{edit.agent}__{edit.path.name}"
        shutil.copy2(edit.path, dest)
        copied.append(dest)
    return copied


def restore_from_archive(plan: MigrationPlan, archive_dir: Path) -> "list[str]":
    """Put every archived original back. Returns the agents restored.

    Best-effort per file and LOUD about any it could not restore: a rollback
    that half-completes must not report success, because that is the
    partially-applied state this module exists to prevent.
    """
    restored: list[str] = []
    for edit in plan.writable:
        src = archive_dir / f"{edit.agent}__{edit.path.name}"
        if not src.is_file():
            logger.error(
                "rollback: no archived original for %s at %s — that spec is "
                "left MIGRATED while others were reverted",
                edit.agent,
                src,
            )
            continue
        try:
            shutil.copy2(src, edit.path)
            restored.append(edit.agent)
        except OSError as exc:  # stx-allow: fallback (report, keep restoring)
            logger.error("rollback: failed to restore %s: %s", edit.agent, exc)
    return restored


def apply_migration(
    plan: MigrationPlan,
    archive_dir: Path,
    verify: "callable",
) -> MigrationResult:
    """Write the plan, verify, and undo if verification fails.

    ``verify`` is called with no arguments AFTER the writes and must return an
    object with a truthy/falsey ``safe`` attribute plus a ``summary()`` — the
    shape :class:`.._hook_arming_diff.HookArmingDiff` already has. Injecting it
    keeps this module free of any opinion about WHAT is verified; it only knows
    that something must be, and what to do when it is not.
    """
    if not plan.safe_to_apply:
        return MigrationResult(refused=f"plan is not safe to apply: {plan.summary()}")
    if not plan.writable:
        return MigrationResult(refused="plan would write nothing")

    archive_originals(plan, archive_dir)

    written: list[str] = []
    for edit in plan.writable:
        edit.path.write_text(edit.new_text or "")
        written.append(edit.agent)

    verdict = verify()
    if not getattr(verdict, "safe", False):
        restored = restore_from_archive(plan, archive_dir)
        detail = verdict.summary() if hasattr(verdict, "summary") else str(verdict)
        logger.error(
            "migration ROLLED BACK: %s (restored %d/%d)",
            detail,
            len(restored),
            len(written),
        )
        return MigrationResult(
            written=(),
            archive_dir=archive_dir,
            rolled_back=f"{detail}; restored {len(restored)}/{len(written)}",
        )

    return MigrationResult(written=tuple(written), archive_dir=archive_dir)


__all__ = [
    "MigrationResult",
    "apply_migration",
    "archive_originals",
    "restore_from_archive",
]
