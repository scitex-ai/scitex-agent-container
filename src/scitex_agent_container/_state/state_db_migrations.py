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


# ``migrate_instances_add_family_tree_cols`` lived here until 2026-08-28.
# It ALTERed ``instances`` to add ``bound_port`` / ``remote`` /
# ``spawned_by``, and that table moved to the shared PostgreSQL store in
# the same change — so it was already written to return early when the
# table is absent, and would have run as a permanent no-op for the rest of
# time. Deleted with the DDL rather than left to be read as a live schema
# step. (``bound_port`` did not even survive the move as a field: it and
# ``a2a_port`` always held one value, and the store keeps one.)


# ``migrate_node_comms_policy_add_group_name`` and
# ``migrate_node_comms_policy_add_group_names`` lived here until
# 2026-08-28. Both ALTERed ``node_comms_policy``, which moved to
# PostgreSQL in the same commit — so both were already written to
# return early when the table is absent, and would have run as
# permanent no-ops for the rest of time. A migration that can never
# fire is not a safety net; it is a claim that a schema step still
# happens. Deleted with the DDL rather than left to be read as live.
