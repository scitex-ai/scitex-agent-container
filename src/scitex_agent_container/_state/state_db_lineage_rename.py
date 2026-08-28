"""Move an agent's lineage edge onto a new name — and refuse when it cannot.

The lineage half of the agent-rename flow. It replaces two entries that
:data:`.._lifecycle._rename_db.NAME_COLUMNS` carried until 2026-08-28,
``("lineage", "child_name")`` and ``("lineage", "parent_name")``, which had
been renamed by a SQLite ``UPDATE`` over a table that no longer exists.

Leaving those pairs behind was not an option, and the reason is the one
:mod:`.._lifecycle._rename_db` already states for ``comms_nodes`` and
``node_comms_policy``: ``rename_rows`` SKIPS a table absent from
``sqlite_master``, so a stale pair does not crash — it reports SUCCESS
while the edge stays under the old name. For lineage that silence is a
PRIVILEGE CHANGE in both directions:

  * the renamed agent's own edge left behind makes the new name a ROOT,
    and a root MAY SPAWN (:func:`.state_db_nodes.spawn_allowed`);
  * its children's edges left behind make each of THEM a root too, on the
    same reasoning, while ``check_lineage_acl`` stops recognising the
    parent's authority over agents it actually spawned.

THE HALF THAT MOVES, AND THE HALF THE SCHEMA WILL NOT LET MOVE
==============================================================
``child_name`` is the store's IDENTITY, so renaming it is not an update: it
is one record ending and another beginning. :func:`rename_lineage` copies
the stored values onto the new identity and RETIRES the old one — the
:func:`.state_db_acl_policy.rename_comms_policy` precedent exactly.

``parent_name`` is IMMUTABLE, and IMMUTABLE means the first value is kept
FOREVER. Not "until a privileged writer overrides it": there is no
override. ``hide`` and ``unhide`` append ops carrying no values, so a
retired-and-restored record comes back with its field stamps intact, and
the next ``put`` merges against them and loses. So an edge that names
``old`` as the parent CANNOT be re-pointed at ``new``. This module does not
pretend otherwise and does not paper over it:

  * It REFUSES the rename, before touching anything, naming every child
    whose edge it cannot move — :exc:`LineageRenameError`, which
    :mod:`.._lifecycle._rename` lets propagate so the whole rename unwinds.
  * It does NOT hide those edges instead. That "works" in the sense that
    nothing is left pointing at a dead name, and it is the worst available
    option: an agent with no edge is a ROOT, so hiding N children's edges
    would hand N agents spawn authority as a side effect of renaming their
    parent. Silent escalation is the failure this whole file exists to
    prevent; doing it deliberately would be worse than doing it by
    accident.

The practical shape of the refusal: an agent that has never spawned
anything renames exactly as before, and an agent that HAS spawned children
cannot be renamed until those edges are dealt with by a human who knows
what the DAG should say. That is a real restriction this migration
introduces, stated here rather than discovered later.
"""

from __future__ import annotations

from typing import Any

__all__ = ["LineageRenameError", "rename_lineage"]


class LineageRenameError(RuntimeError):
    """A lineage edge could not follow the rename. NOTHING was changed.

    Raised before any write, so the store is exactly as it was and the
    caller's unwind has nothing to undo for this step.
    """


def rename_lineage(*, old: str, new: str) -> bool:
    """Move ``old``'s own lineage edge onto ``new``. ``True`` iff one moved.

    Two things happen, in this order, and the order is the safety property:

    1. REFUSE FIRST. If any edge names ``old`` as a PARENT, raise
       :exc:`LineageRenameError` naming those children. ``parent_name`` is
       IMMUTABLE and cannot be re-pointed (see the module docstring), and
       the alternatives — leaving them, or hiding them — both grant spawn
       authority to agents that should not have it. Nothing is written.
    2. MOVE THE CHILD-SIDE EDGE. If ``old`` is itself a child, copy its
       ``parent_name`` and ``created_at`` verbatim onto the ``new``
       identity and hide the ``old`` record. ``created_at`` is carried, NOT
       re-stamped: the spawn happened when it happened, and the field is
       IMMUTABLE, so a re-stamp would be both a lie and a conflict.

    Idempotent in the useful sense: with nothing live under ``old`` it
    returns ``False`` and writes nothing, so a re-run after a partial
    rename does not clobber the record already sitting under ``new``.

    The inverse is ``rename_lineage(old=new, new=old)``, which is what
    :mod:`.._lifecycle._rename` pushes onto its undo stack.
    """
    if not old or not new or old == new:
        return False

    from scitex_dev.store import ANY_REVISION

    from .state_db_lineage_store import ACTOR, run_with_reconnect

    def _move(store: Any) -> bool:
        orphaned = sorted(
            str(row.values["child_name"])
            for row in store.rows()
            if str(row.values["parent_name"]) == old
        )
        if orphaned:
            raise LineageRenameError(
                f"cannot rename {old!r} to {new!r}: "
                f"{len(orphaned)} lineage edge(s) name it as the PARENT "
                f"({', '.join(orphaned)}), and ``parent_name`` is IMMUTABLE "
                f"in the lineage store — the first value is kept forever, so "
                f"those edges cannot be re-pointed. Leaving them would make "
                f"each of those agents a ROOT (a root may spawn); hiding "
                f"them would do the same. Nothing was changed. Resolve the "
                f"DAG for those children first, or rename a leaf."
            )

        row = store.get({"child_name": old})
        if row is None:
            return False
        moved = {
            "child_name": new,
            "parent_name": str(row.values["parent_name"]),
            # Verbatim. The edge is a historical fact and the field is
            # IMMUTABLE; re-stamping would rewrite when the spawn happened.
            "created_at": float(row.values["created_at"]),
        }
        if store.is_hidden({"child_name": new}):
            store.unhide(
                {"child_name": new}, expected_revision=ANY_REVISION, actor=ACTOR
            )
        store.put(moved, expected_revision=ANY_REVISION, actor=ACTOR)
        store.hide({"child_name": old}, expected_revision=ANY_REVISION, actor=ACTOR)
        return True

    return bool(run_with_reconnect(_move))

# EOF
