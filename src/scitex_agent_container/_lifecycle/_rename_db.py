#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rename an agent's rows in ``state.db`` — reversibly.

The agent name is a foreign key by convention in a dozen tables (there
are no real FKs on it). A rename that moves the spec dir but leaves
``comms_nodes.name`` / ``node_comms_policy.name`` / ``lineage`` pointing
at the old name produces an agent that starts but cannot be addressed:
the A2A directory still advertises the dead name, and the ACL gate has no
policy row for the live one.

So: rename EVERY row in state.db that keys on the name, including the
history (``attempts``, ``channel_events``). A renamed agent is the SAME
agent — its past must still be findable under the new name. This is the
``git mv`` position: the name changed, history follows.

WHAT THIS NO LONGER COVERS, STATED RATHER THAN LEFT AS A DEAD ENTRY
===================================================================
``turns`` / ``errors`` / ``heartbeats`` were in :data:`NAME_COLUMNS`
until 2026-08-28. They are no longer SQLite tables — the diary moved to
per-host PostgreSQL (:mod:`.._state.state_db_diary`) — and the loops
below skip a table that does not exist, so those three entries could
only ever match zero rows. They are REMOVED rather than left as
reassuring decoration: a list naming a table this code cannot reach
reads as coverage that is not there.

The diary history therefore does NOT follow a rename today, and it
cannot be made to follow one by adding a store call here. ``name`` is an
IDENTITY field of the heartbeats store (changing it names a different
record, not the same one renamed) and an ``IMMUTABLE`` data field of the
turns and errors stores, so the store REFUSES the write by design. Making
diary history follow a rename means hiding each record and re-writing it
under a new identity — a decision about append-only history, not a line
of config, and deliberately not smuggled into this migration.

Reversibility: we capture the ``rowid`` of every row we are about to
touch, BEFORE touching it. The undo is then a rowid-scoped UPDATE back to
the old value — exact, and immune to the trap a naive
``UPDATE … WHERE name = new`` would hit (it would also clobber rows that
already held ``new``, e.g. the history of a previously-deleted agent that
happened to have that name).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ._rename_spec import sub_path

# (table, column) pairs holding an agent NAME verbatim.
NAME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("definitions", "name"),
    ("instances", "name"),
    ("instances", "spawned_by"),
    ("attempts", "agent"),
    # ("turns", "name") / ("errors", "name") / ("heartbeats", "name") were
    # here until 2026-08-28 — see the module docstring for why removing them
    # is the honest edit and why a store call cannot replace them.
    ("channel_events", "target"),
    ("channel_events", "source"),
    ("node_tokens", "name"),
    ("lineage", "child_name"),
    ("lineage", "parent_name"),
    ("comms_grants", "sender_name"),
    ("comms_grants", "target_name"),
    ("comms_nodes", "name"),
    # ("node_comms_policy", "name") was here until 2026-08-28. That table
    # moved to PostgreSQL, and leaving the pair would have been WORSE than
    # a crash: ``rename_rows`` skips tables absent from sqlite_master, so
    # the rename would have reported success while the policy stayed under
    # the OLD name. The renamed agent then resolves to NO named group and
    # every authority gate denies it. The move is done by
    # ``state_db_acl_policy.rename_comms_policy``, called as its own step
    # in :mod:`._rename` with its own inverse on the undo stack.
)

# (table, column) pairs holding a PATH that embeds the agent name as a
# component (``…/agents/<name>/spec.yaml``, ``…/proj/<name>``).
PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("definitions", "yaml_path"),
    ("instances", "workdir"),
)


class DbRenameError(RuntimeError):
    """A state.db row rename failed. Nothing was committed."""


@dataclass
class DbUndo:
    """Rowid-scoped inverse of a completed :func:`rename_rows`."""

    db_path: Path
    old: str
    new: str
    # (table, column) -> rowids we updated
    name_rows: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    path_rows: dict[tuple[str, str], list[tuple[int, str]]] = field(
        default_factory=dict
    )

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.name_rows.values()) + sum(
            len(v) for v in self.path_rows.values()
        )


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


def count_rows(db_path: Path, old: str) -> dict[str, int]:
    """Return ``{"table.column": n}`` for every row a rename would touch.

    Read-only — this is what ``--dry-run`` prints. A missing DB file or a
    table that does not exist yet contributes nothing.
    """
    if not Path(db_path).is_file():
        return {}
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        tables = _existing_tables(conn)
        for table, column in NAME_COLUMNS:
            if table not in tables:
                continue
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (old,)  # noqa: S608
            ).fetchone()[0]
            if n:
                counts[f"{table}.{column}"] = n
        for table, column in PATH_COLUMNS:
            if table not in tables:
                continue
            n = sum(
                1
                for (value,) in conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} LIKE ?",  # noqa: S608
                    (f"%{old}%",),
                )
                if _has_component(value, old)
            )
            if n:
                counts[f"{table}.{column}"] = n
    finally:
        conn.close()
    return counts


def _has_component(value: object, old: str) -> bool:
    """True when ``value`` is a path with ``old`` as a whole component."""
    return isinstance(value, str) and old in value.split("/")


def rename_rows(db_path: Path, old: str, new: str) -> DbUndo:
    """Point every ``old`` row at ``new``, in ONE transaction.

    Returns a :class:`DbUndo` the caller stashes on its rollback stack.
    A missing DB file is a no-op (an empty undo) — a fleet that has never
    started an agent has no state.db, and that must not block a rename.

    Raises:
        DbRenameError: A UNIQUE constraint rejected the new name (a row
            for ``new`` already exists — typically the leftovers of a
            previously deleted agent by that name). Nothing is committed.
    """
    undo = DbUndo(db_path=Path(db_path), old=old, new=new)
    if not Path(db_path).is_file():
        return undo

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = _existing_tables(conn)

        for table, column in NAME_COLUMNS:
            if table not in tables:
                continue
            rowids = [
                r[0]
                for r in conn.execute(
                    f"SELECT rowid FROM {table} WHERE {column} = ?",  # noqa: S608
                    (old,),
                )
            ]
            if not rowids:
                continue
            _update_rowids(conn, table, column, rowids, new)
            undo.name_rows[(table, column)] = rowids

        for table, column in PATH_COLUMNS:
            if table not in tables:
                continue
            rewritten = [
                (rowid, value, sub_path(value, old, new))
                for rowid, value in conn.execute(
                    f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE ?",  # noqa: S608
                    (f"%{old}%",),
                )
                if isinstance(value, str)
            ]
            touched = [(r, b, a) for r, b, a in rewritten if a != b]
            if not touched:
                continue
            for rowid, _before, after in touched:
                _update_rowids(conn, table, column, [rowid], after)
            undo.path_rows[(table, column)] = [(r, b) for r, b, _a in touched]

        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DbRenameError(
            f"state.db already holds rows for {new!r} — a previously deleted "
            f"agent by that name likely left them behind ({exc}). Nothing was "
            f"changed. Inspect with: sac db query --agent {new}"
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return undo


def undo_rename_rows(undo: DbUndo) -> None:
    """Restore every row :func:`rename_rows` touched, by rowid."""
    if undo.total == 0 or not undo.db_path.is_file():
        return
    conn = sqlite3.connect(str(undo.db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for (table, column), rowids in undo.name_rows.items():
            _update_rowids(conn, table, column, rowids, undo.old)
        for (table, column), pairs in undo.path_rows.items():
            for rowid, before in pairs:
                _update_rowids(conn, table, column, [rowid], before)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update_rowids(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    rowids: list[int],
    value: str,
) -> None:
    """``UPDATE <table> SET <column> = ? WHERE rowid IN (…)``.

    ``table`` / ``column`` come from the module-level constants above —
    never from user input — so interpolating them is safe; the VALUES are
    always bound.
    """
    placeholders = ",".join("?" for _ in rowids)
    conn.execute(
        f"UPDATE {table} SET {column} = ? WHERE rowid IN ({placeholders})",  # noqa: S608
        (value, *rowids),
    )


__all__ = [
    "NAME_COLUMNS",
    "PATH_COLUMNS",
    "DbRenameError",
    "DbUndo",
    "count_rows",
    "rename_rows",
    "undo_rename_rows",
]
