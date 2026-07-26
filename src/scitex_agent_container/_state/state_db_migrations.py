"""Idempotent schema migrations for state.db.

Extracted from :mod:`state_db` so that module stays under the per-file
line cap. Each function takes an open :class:`sqlite3.Connection` and is
a no-op when its migration has already run, so they are safe to call on
every :func:`state_db.init_schema`.

  * :func:`migrate_legacy_heartbeats` — rename the original F-CS11
    instance-tied ``heartbeats`` table to ``instance_heartbeats`` so the
    diary-style ``heartbeats`` can own the canonical name.
  * :func:`migrate_instance_heartbeats_add_seq` — rebuild
    ``instance_heartbeats`` to add the monotonic ``seq`` PK that makes
    "latest heartbeat" deterministic (see the table DDL in
    :mod:`state_db` and :mod:`state_db_heartbeats`).
  * :func:`migrate_instances_add_family_tree_cols` — ADD COLUMN the
    sac-agent-spawn family-tree columns (``bound_port``, ``remote``,
    ``spawned_by``) onto a pre-existing ``instances`` table.
  * :func:`migrate_instances_add_launch_identity_cols` — ADD COLUMN the
    selected profile, harness, backend, and model identity.
  * :func:`migrate_node_comms_policy_add_group_name` — ADD COLUMN the
    ``group_name`` column (group-based ACL, operator 2026-06-25) onto a
    pre-existing ``node_comms_policy`` table.
"""

from __future__ import annotations

import sqlite3


def migrate_legacy_heartbeats(conn: sqlite3.Connection) -> None:
    """Rename the legacy F-CS11 ``heartbeats`` table → ``instance_heartbeats``.

    Detection: legacy schema has an ``instance_id`` column (NOT NULL,
    REFERENCES instances). The diary schema has no such column. We
    only rename when:

      * a table called ``heartbeats`` exists, AND
      * that table has an ``instance_id`` column, AND
      * ``instance_heartbeats`` does NOT already exist.

    Idempotent: re-running on an already-migrated DB is a no-op.
    """
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "heartbeats" not in existing:
        return
    if "instance_heartbeats" in existing:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(heartbeats)").fetchall()}
    if "instance_id" not in cols:
        # Already the diary schema (no rename needed).
        return
    conn.execute("ALTER TABLE heartbeats RENAME TO instance_heartbeats")


def migrate_instance_heartbeats_add_seq(conn: sqlite3.Connection) -> None:
    """Rebuild ``instance_heartbeats`` to add the monotonic ``seq`` PK.

    The pre-``seq`` table had ``PRIMARY KEY (instance_id, ts)`` with a
    second-resolution ``ts``, so "latest heartbeat" was non-deterministic
    whenever two beats straddled a second boundary. The new shape adds
    ``seq INTEGER PRIMARY KEY AUTOINCREMENT`` (total insertion order) and
    demotes ``(instance_id, ts)`` to ``UNIQUE`` (still collapses
    same-second beats). SQLite cannot ALTER-add an AUTOINCREMENT PK, so
    we rebuild: create the new table, copy rows ordered by the old
    ``rowid`` (preserves arrival order → ``seq`` is monotonic in arrival),
    drop the old, rename.

    Detection: ``instance_heartbeats`` exists AND lacks a ``seq`` column.
    Idempotent: a no-op once ``seq`` is present (or the table is absent).
    """
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "instance_heartbeats" not in existing:
        return
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(instance_heartbeats)").fetchall()
    }
    if "seq" in cols:
        return
    conn.executescript(
        """
        CREATE TABLE instance_heartbeats__new (
            seq             INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     TEXT NOT NULL REFERENCES instances(id),
            ts              TEXT NOT NULL,
            iter            INTEGER,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            pane_state      TEXT,
            UNIQUE (instance_id, ts)
        );
        INSERT INTO instance_heartbeats__new
            (instance_id, ts, iter, input_tokens, output_tokens, pane_state)
        SELECT instance_id, ts, iter, input_tokens, output_tokens, pane_state
        FROM instance_heartbeats
        ORDER BY rowid;
        DROP TABLE instance_heartbeats;
        ALTER TABLE instance_heartbeats__new RENAME TO instance_heartbeats;
        """
    )


def migrate_instances_add_family_tree_cols(conn: sqlite3.Connection) -> None:
    """ADD the sac-agent-spawn family-tree columns to ``instances``.

    New columns (see :mod:`state_db` DDL + :mod:`state_db_instances`):

      * ``bound_port`` INTEGER — the actual bound a2a port.
      * ``remote``     INTEGER DEFAULT 0 — 1 for a cross-host agent.
      * ``spawned_by`` TEXT — launching identity (lineage edge).

    A fresh DB gets these from the ``CREATE TABLE`` DDL; this migration
    is for an EXISTING ``instances`` table created before the columns
    existed. ``ALTER TABLE ... ADD COLUMN`` is cheap and SQLite-native.

    Detection: ``instances`` exists AND is missing one of the three
    columns. Per-column guarded so a partially-migrated DB completes.
    Idempotent: a no-op once all three columns are present (or the
    table is absent).
    """
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "instances" not in existing:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
    if "bound_port" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN bound_port INTEGER")
    if "remote" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN remote INTEGER DEFAULT 0")
    if "spawned_by" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN spawned_by TEXT")


def migrate_instances_add_launch_identity_cols(conn: sqlite3.Connection) -> None:
    """Add effective launch-profile identity to pre-existing instances."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "instances" not in existing:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(instances)").fetchall()}
    for column in ("profile", "harness", "backend", "model"):
        if column not in cols:
            conn.execute(f"ALTER TABLE instances ADD COLUMN {column} TEXT")


def migrate_node_comms_policy_add_group_name(conn: sqlite3.Connection) -> None:
    """ADD the ``group_name`` column to ``node_comms_policy``.

    Group-based ACL (operator 2026-06-25): the per-agent NAMED group,
    resolved at ``agent_start`` from ``metadata.labels.group`` (else
    role-derived). A fresh DB gets the column from the ``CREATE TABLE``
    DDL in :mod:`state_db`; this migration is for an EXISTING
    ``node_comms_policy`` table created before the column existed.

    ``ALTER TABLE ... ADD COLUMN`` with a ``DEFAULT ''`` backfills every
    pre-existing row to "ungrouped", which is byte-equivalent to the
    pre-group-name behaviour (the ACL same-group allow requires a
    NON-EMPTY match).

    Detection: ``node_comms_policy`` exists AND lacks ``group_name``.
    Idempotent: a no-op once the column is present (or the table is
    absent).
    """
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "node_comms_policy" not in existing:
        return
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(node_comms_policy)").fetchall()
    }
    if "group_name" in cols:
        return
    conn.execute(
        "ALTER TABLE node_comms_policy ADD COLUMN group_name TEXT NOT NULL DEFAULT ''"
    )
