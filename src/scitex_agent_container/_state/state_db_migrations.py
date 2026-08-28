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
  * :func:`migrate_node_comms_policy_add_group_name` — ADD COLUMN the
    ``group_name`` column (group-based ACL, operator 2026-06-25) onto a
    pre-existing ``node_comms_policy`` table.
  * :func:`migrate_node_comms_policy_add_group_names` — ADD COLUMN the
    MULTI-value ``group_names`` column (authority-is-membership,
    incident 2026-08-10) onto a pre-existing ``node_comms_policy``.

``migrate_instances_add_family_tree_cols`` WAS HERE AND IS GONE
(2026-08-28). It ADD-COLUMNed ``bound_port`` / ``remote`` /
``spawned_by`` onto a pre-existing ``instances`` table with SQLite-native
``ALTER TABLE`` statements. ``instances`` moved to PostgreSQL, so the
migration had nothing left to migrate — its own guard (``if "instances"
not in existing: return``) meant it would have gone on running forever as
a silent no-op, which is worse than deleting it: a migration that cannot
fire still reads, to the next person, as evidence that the table it names
is still here. The three fields it backfilled are now ordinary schema
fields; the store applies additive schema changes itself
(``Store._apply_additive_migrations``), so there is no successor function
to write.
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
            -- No REFERENCES instances(id): that table is in another
            -- engine since 2026-08-28, and rebuilding this one with a
            -- foreign key SQLite cannot check would make the rebuild
            -- FAIL under `PRAGMA foreign_keys = ON` (which open_db sets).
            instance_id     TEXT NOT NULL,
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


def migrate_node_comms_policy_add_group_names(conn: sqlite3.Connection) -> None:
    """ADD the MULTI-value ``group_names`` column to ``node_comms_policy``.

    Authority-is-membership (incident 2026-08-10): ``group_name`` holds
    only the FIRST group a spec's ``metadata.labels.groups`` list names,
    so an agent authored as ``groups: [generalist, developer]`` was not a
    developer to any ACL gate. ``group_names`` holds the WHOLE set
    (comma-separated, sorted, written from the same labels), and the
    authority predicates read it.

    ``ALTER TABLE ... ADD COLUMN`` with ``DEFAULT ''`` leaves every
    pre-existing row with an empty set. That is deliberately NOT a
    regression: :func:`.state_db_groups.resolve_group_names` unions the
    set with ``group_name``, so an un-refreshed row still resolves to
    ``{primary}`` — exactly the pre-migration behaviour. Running
    ``sac agents refresh-acl`` (or restarting the agent) re-publishes the
    full set from the on-disk spec.

    Detection: ``node_comms_policy`` exists AND lacks ``group_names``.
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
    if "group_names" in cols:
        return
    conn.execute(
        "ALTER TABLE node_comms_policy ADD COLUMN group_names TEXT NOT NULL DEFAULT ''"
    )
