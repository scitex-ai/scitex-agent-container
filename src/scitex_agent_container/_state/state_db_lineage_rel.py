"""``sender → target`` lineage classification — ON POSTGRESQL.

Extracted from :mod:`.state_db_acl_policy` on 2026-08-28, when the policy
table moved to PostgreSQL and this function did not: it reads a DIFFERENT
table, ``lineage``, which had its own migration ahead of it. That migration
landed the same day, so the note this module used to carry — "STILL ON
THE OLD BACKEND ... it kept ``db_path``, and kept reading a local file, until ``lineage``
itself moves" — is now discharged rather than merely stale. ``db_path`` is
gone; there is no file to point it at.

The file stays, and it earns its keep for the reason it was split out in
the first place: the classification is a distinct question from the policy
that consumes it. :mod:`.state_db_acl_policy` answers "what is this agent
allowed to do"; this answers "what IS this agent to that one".

Re-exported from :mod:`.state_db_acl_policy` (and thence from
:mod:`.state_db_nodes`) so every existing import path keeps working.
"""

from __future__ import annotations

__all__ = ["sender_target_relationship"]


def sender_target_relationship(
    *,
    sender: str,
    target: str,
) -> str:
    """Classify the ``sender → target`` lineage relationship.

    Returns one of:

    * ``"self"``    — same node (trivial self-send).
    * ``"parent"``  — target is sender's parent in the lineage store.
    * ``"child"``   — target is one of sender's direct children.
    * ``"sibling"`` — sender and target share the same parent.
    * ``"other"``   — no lineage path (cross-group or unrelated).

    Used by :func:`scitex_agent_container._listen._acl.check_send_acl`
    to apply the per-spec outbound/inbound policy on the right edge.
    Pure read of the lineage store — no policy state consulted.

    TWO POINT READS, NOT A SCAN. Every one of the five answers is decided
    by the two parents alone: ``target`` being ``sender``'s parent, or
    ``sender`` being ``target``'s, or the two agreeing. ``child_name`` is
    the store's identity, so each is an indexed ``get`` — the same two
    round-trips the previous implementation made, against a table that no longer has
    to exist on this host to be readable.

    ``"self"`` is answered BEFORE either read, so the commonest trivial
    case never touches the store at all.
    """
    if not sender or not target:
        return "other"
    if sender == target:
        return "self"

    from .state_db_lineage_store import parent_name_of

    sender_parent = parent_name_of(sender)
    target_parent = parent_name_of(target)
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

# EOF
