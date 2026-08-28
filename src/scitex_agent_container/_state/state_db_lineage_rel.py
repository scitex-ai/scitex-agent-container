"""``sender → target`` lineage classification — STILL ON SQLITE.

Extracted from :mod:`.state_db_acl_policy` on 2026-08-28, when the policy
table moved to PostgreSQL and this function did not.

IT DID NOT MOVE BECAUSE IT READS A DIFFERENT TABLE. ``lineage`` is owned
by :mod:`.state_db_nodes` (``record_lineage`` / ``derive_group``) and has
its own migration ahead of it. This function only ever lived beside the
policy code because ``state_db_nodes`` was over the per-file line cap,
and carrying its storage along as a side effect of the policy move would
have been a second migration smuggled inside the first.

So it keeps ``db_path``, and keeps reading SQLite, until ``lineage``
itself moves. Its own file is what makes that visible: a reader looking
for "what is left on SQLite here" finds a module, not a stray function at
the bottom of a PostgreSQL one.

Re-exported from :mod:`.state_db_acl_policy` (and thence from
:mod:`.state_db_nodes`) so every existing import path keeps working.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["sender_target_relationship"]


def sender_target_relationship(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> str:
    """Classify the ``sender → target`` lineage relationship.

    Returns one of:

    * ``"self"``    — same node (trivial self-send).
    * ``"parent"``  — target is sender's parent in the lineage table.
    * ``"child"``   — target is one of sender's direct children.
    * ``"sibling"`` — sender and target share the same parent.
    * ``"other"``   — no lineage path (cross-group or unrelated).

    Used by :func:`scitex_agent_container._listen._acl.check_send_acl`
    to apply the per-spec outbound/inbound policy on the right edge.
    Pure read of the ``lineage`` table — no policy state consulted.
    """
    if not sender or not target:
        return "other"
    if sender == target:
        return "self"
    from .state_db import open_db

    with open_db(db_path) as conn:
        sender_parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (sender,)
        ).fetchone()
        target_parent_row = conn.execute(
            "SELECT parent_name FROM lineage WHERE child_name = ?", (target,)
        ).fetchone()
    sender_parent = str(sender_parent_row["parent_name"]) if sender_parent_row else None
    target_parent = str(target_parent_row["parent_name"]) if target_parent_row else None
    if sender_parent == target:
        return "parent"
    if target_parent == sender:
        return "child"
    if (
        sender_parent is not None
        and target_parent is not None
        and sender_parent == target_parent
    ):
        return "sibling"
    return "other"
