"""Theme 15 — explicit (node, group) join table over ``comms_node_groups``.

ADR-0010 Step 2 (``node_comms_policy.lineage_group``) was a single-
label column: each node belongs to AT MOST one lineage group. The
groups-mesh self-register design (Theme 15) needs a node to declare
MEMBERSHIP IN MULTIPLE GROUPS so cross-cohort discovery and
group-broadcast features can scale beyond the single-lineage shape.

This module owns:

* :func:`migrate_node_groups_split` — idempotent backfill of the
  legacy ``node_comms_policy.lineage_group`` column into the new
  :table:`comms_node_groups` join. Reserved discriminants (``''`` —
  the parsed default for "no group", and ``'solitary'`` — the ACL's
  derive-only sentinel) are EXCLUDED so the join table only carries
  intentional memberships. Re-running the migration after a populated
  state.db is a no-op (uses ``INSERT OR IGNORE`` against the natural
  ``(node_name, group_name)`` PK).
* :func:`has_shared_group` — the ACL-side group-overlap check that
  supersedes the pre-Theme-15 ``derive_group`` reading of the
  ``lineage_group`` column. The explicit join wins when present;
  otherwise we synthesise a SINGLETON from the legacy column so
  un-migrated nodes still produce the byte-equivalent ACL answer.

The ``comms_node_groups`` table DDL + ``KNOWN_TABLES`` entry both
live in :mod:`scitex_agent_container._state.state_db` so a fresh
``init_schema`` includes the join table. This module only owns the
data semantics (migration + lookup), not the schema itself.

Out of scope (later cuts):

* CLI surface (``sac db query --table=comms_node_groups`` already
  works via the ``KNOWN_TABLES`` whitelist; a writer subcommand will
  follow the read-only landing).
* Group-broadcast ACL extensions (the join table feeds the future
  ``send_to_group`` path; for now ``has_shared_group`` is only read
  by the existing per-pair ACL check that already consumes
  ``derive_group``).
* Removing the legacy ``lineage_group`` column — the column stays for
  one ecosystem release so v3 ACL paths and the migration both work
  on the same DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Reserved values of ``node_comms_policy.lineage_group`` that MUST NOT
# be promoted to explicit ``comms_node_groups`` rows.
#
# * ``""`` — the parsed default for "no group declared in spec.yaml".
#   Promoting it would create a "no-group group" that every fresh
#   node falls into, which is the opposite of the join table's
#   purpose (only intentional memberships).
# * ``"solitary"`` — the ACL's derive-only sentinel (``derive_group``
#   reads this as "node is alone — no peers"). Promoting it would
#   collide every solitary node into a single "solitary" supergroup
#   and silently break the isolation semantic.
#
# Kept as a frozenset (not a list / tuple) because membership-testing
# is the only operation and the set's ``in`` is O(1) — the migration
# checks every legacy row.
_RESERVED_LINEAGE_VALUES: frozenset[str] = frozenset({"", "solitary"})


def migrate_node_groups_split(conn: sqlite3.Connection) -> int:
    """Backfill legacy ``node_comms_policy.lineage_group`` into the join table.

    For every ``node_comms_policy`` row whose ``lineage_group`` is NOT
    reserved (see :data:`_RESERVED_LINEAGE_VALUES`), insert a single
    ``comms_node_groups`` row ``(name, lineage_group)`` using the
    legacy row's ``updated_at`` as ``created_at`` (so the join table
    timeline matches the source-of-truth timeline).

    Idempotent: the ``INSERT OR IGNORE`` against the
    ``(node_name, group_name)`` primary key means re-running on a DB
    that has ALREADY been migrated promotes zero new rows. The return
    value is the number of rows actually inserted on THIS call (0 on
    a no-op second run).

    Caller owns the transaction — we don't ``commit()`` so callers
    can pipeline this migration with other DDL/data fixes in one
    txn (the eventual ecosystem-wide migration script will batch it
    with sibling moves).
    """
    cur = conn.execute(
        """
        SELECT name, lineage_group, updated_at
        FROM node_comms_policy
        WHERE lineage_group NOT IN ('', 'solitary')
        """
    )
    rows = cur.fetchall()
    inserted = 0
    for name, group, updated_at in rows:
        # Defensive recheck — keep the SQL filter and the Python
        # filter in sync; a typo in either is loudly caught here.
        if group in _RESERVED_LINEAGE_VALUES:
            continue
        result = conn.execute(
            """
            INSERT OR IGNORE INTO comms_node_groups
                (node_name, group_name, created_at)
            VALUES (?, ?, ?)
            """,
            (name, group, updated_at),
        )
        inserted += result.rowcount or 0
    return inserted


def has_shared_group(
    *,
    a: str,
    b: str,
    db_path: Path,
) -> bool:
    """Return True iff nodes *a* and *b* share at least one group.

    Lookup order (matches the pre-Theme-15 ``derive_group`` semantics
    for un-migrated nodes):

    1. EXPLICIT — both nodes have at least one row in
       ``comms_node_groups`` AND those row sets intersect. This is the
       new (multi-group) shape.
    2. LEGACY-SINGLETON fallback — if EITHER node has no explicit
       ``comms_node_groups`` row, fall through to the
       ``node_comms_policy.lineage_group`` column and treat the value
       as a SINGLETON group (the byte-equivalent of the pre-Theme-15
       view). Reserved discriminants (``''`` / ``'solitary'``) never
       compare equal — they collapse to the "no shared group" answer.

    The fallback is critical for the rolling migration: nodes that
    have NOT yet been migrated must still produce the same ACL answer
    the pre-Theme-15 ``derive_group`` produced, so the migration is
    invisible to ACL behaviour.

    Connects on its own (matches the rest of state_db's per-call
    connection style) — the read is fast, and the caller chain is
    synchronous-but-rare (one call per send-ACL check).
    """
    with sqlite3.connect(db_path) as conn:
        explicit = _shared_explicit_group(conn, a, b)
        if explicit is not None:
            return explicit
        return _shared_legacy_singleton(conn, a, b)


def _shared_explicit_group(conn: sqlite3.Connection, a: str, b: str) -> bool | None:
    """Return True/False from the explicit join, or None to defer.

    None signals "neither node has an explicit row, so let the
    legacy-singleton fallback answer" — the caller chains to
    :func:`_shared_legacy_singleton`. Returning ``False`` straight
    from the explicit path when ONE node has rows and the other
    doesn't would mask the legacy fallback for the un-migrated side;
    we only return a hard answer when BOTH sides have explicit data.
    """
    a_groups = {
        row[0]
        for row in conn.execute(
            "SELECT group_name FROM comms_node_groups WHERE node_name = ?",
            (a,),
        )
    }
    b_groups = {
        row[0]
        for row in conn.execute(
            "SELECT group_name FROM comms_node_groups WHERE node_name = ?",
            (b,),
        )
    }
    if not a_groups and not b_groups:
        return None  # defer to legacy
    if not a_groups or not b_groups:
        # ONE side has explicit rows, the other doesn't. Mixed-state:
        # the rolling migration may have caught one node and not the
        # other. Defer to legacy so the un-migrated side can still
        # contribute its singleton — has_shared_group will OR the
        # explicit set against the other side's legacy value.
        return _mixed_explicit_legacy(conn, a, a_groups, b, b_groups)
    return bool(a_groups & b_groups)


def _mixed_explicit_legacy(
    conn: sqlite3.Connection,
    a: str,
    a_groups: set[str],
    b: str,
    b_groups: set[str],
) -> bool:
    """One side has explicit rows, the other only legacy. Cross-check."""
    if a_groups:
        legacy_b = _legacy_group(conn, b)
        return legacy_b in a_groups if legacy_b else False
    legacy_a = _legacy_group(conn, a)
    return legacy_a in b_groups if legacy_a else False


def _shared_legacy_singleton(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Read ``lineage_group`` for both; share iff equal and non-reserved."""
    legacy_a = _legacy_group(conn, a)
    legacy_b = _legacy_group(conn, b)
    if not legacy_a or not legacy_b:
        return False
    return legacy_a == legacy_b


def _legacy_group(conn: sqlite3.Connection, name: str) -> str | None:
    """Return the legacy ``lineage_group`` or None for reserved values.

    Reserved discriminants (``''`` / ``'solitary'``) collapse to
    None so the caller's "share iff equal" check never accidentally
    matches two solitary nodes as a shared group.
    """
    row = conn.execute(
        "SELECT lineage_group FROM node_comms_policy WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    if value in _RESERVED_LINEAGE_VALUES:
        return None
    return value


__all__ = ["has_shared_group", "migrate_node_groups_split"]
