"""Is the state store ABSENT, EMPTY, SCHEMA-LESS, or POPULATED? — four states.

WHY THIS EXISTS. ``open_db`` calls ``init_schema`` unconditionally, so opening
a path that is wrong, or a file that is zero bytes, SILENTLY CREATES the schema
there and every subsequent query returns zero rows. The fleet then reads as
"no agents registered" — a confident, well-formed answer produced by looking at
the wrong thing.

Measured 2026-08-09: a 0-byte ``state.db`` at the pre-cutover path was accepted
by ``sqlite3.connect``, ``init_schema`` built the tables on it, and every query
returned nothing. The same day, ``sac agents list`` reported 6 of 21 agents and
``a2a_peers`` reported zero while TWELVE agents were running and answering on
that very rail. An empty read was taken as a fact about the world when it was a
fact about the reader.

THE FOUR STATES, and they need four different responses:

    absent      the file does not exist              -> wrong path, or nothing
                                                        has ever written here
    empty       exists, zero bytes / no SQLite header -> a placeholder, a failed
                                                        create, or a truncated
                                                        file. NOT a database.
    schemaless  a real SQLite file, but OUR tables    -> some other database, or
                are missing                             a pre-init file
    populated   our tables are present                -> the only state in which
                                                        "zero agents" means
                                                        zero agents

Collapsing the first three into "zero rows" is the defect. Only ``populated``
licenses a factual claim about fleet contents.

THIS MODULE NEVER CREATES ANYTHING. ``inspect_store`` opens read-only and does
not call ``init_schema`` — a diagnostic that has the side effect causing the bug
would be useless. It is safe to call on a suspected-wrong path.

Design note carried from :mod:`.state_db_groups` (``GroupResolution``): the
three/four-state discipline belongs at the REPORTING boundary, not everywhere.
Writers still call ``open_db`` and still get auto-init, which is correct for
them. This is for the readers that report to a human.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

#: The four distinguishable states of the store.
STORE_STATES = ("absent", "empty", "schemaless", "populated")

#: Tables whose presence means "this is our state store". Deliberately a
#: SMALL core rather than the full schema: a store mid-migration may lack a
#: newer table without being a different database.
#:
#: It was ``("instances", "definitions")`` until 2026-08-28, and every other
#: member it has ever had left SQLite that same day — ``definitions`` for
#: having no writer, ``lineage`` and ``instances`` for the shared PostgreSQL
#: store. Each had to go for the same reason: this probe reads
#: ``sqlite_master``, so a name SQLite no longer creates would make every
#: healthy state.db report ``schemaless`` — the probe's own "this is not our
#: database" verdict, produced by our own migration.
#:
#: THE CORE IS DOWN TO ONE, and the previous note argued against exactly
#: that ("``channel_events`` replaces it rather than leaving a one-element
#: core"). That argument was about CHOOSING to shrink; this is not a choice.
#: ``channel_events`` is the only table ``init_schema`` still creates, so a
#: second name would have to be one SQLite no longer has — which is the
#: failure the paragraph above describes, not a hedge against it. The
#: predicate stays ANY so the shape survives the next table arriving.
CORE_TABLES = ("channel_events",)

#: The 16-byte magic every SQLite file starts with.
_SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class StoreState:
    """What the store at ``path`` actually is, and what to do about it."""

    path: Path
    state: str
    tables: tuple[str, ...] = ()
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if self.state not in STORE_STATES:
            raise ValueError(
                f"unknown store state {self.state!r}; expected one of {STORE_STATES}"
            )

    @property
    def is_populated(self) -> bool:
        """True only when a row count may be reported as a fact."""
        return self.state == "populated"

    def describe(self) -> str:
        """One clause naming the state AND the remedy."""
        if self.state == "populated":
            return f"state store at {self.path} carries {len(self.tables)} table(s)"
        if self.state == "absent":
            return (
                f"NO state store exists at {self.path} — nothing has ever "
                "written here, or the path is wrong. Any 'zero agents' reading "
                "from this path describes the PATH, not the fleet"
            )
        if self.state == "empty":
            return (
                f"the file at {self.path} is {self.size_bytes} bytes and is NOT "
                "a SQLite database — a placeholder or a failed/truncated "
                "create. sqlite3.connect accepts it and schema-init will "
                "happily build empty tables on top, which is how this becomes "
                "an invisible 'empty fleet'"
            )
        return (
            f"the file at {self.path} is a SQLite database but carries none of "
            f"{list(CORE_TABLES)} — it is a DIFFERENT database, or one that has "
            "never been initialised. Verify the path before reading anything "
            "from it as fleet state"
        )


def _table_names(path: Path) -> tuple[str, ...]:
    """Tables in ``path``, read-only. Empty tuple if unreadable as SQLite."""
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return ()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def inspect_store(db_path: Path) -> StoreState:
    """Classify the store at ``db_path`` WITHOUT creating or migrating it."""
    path = Path(db_path)
    if not path.exists():
        return StoreState(path=path, state="absent")

    size = path.stat().st_size
    if size == 0:
        return StoreState(path=path, state="empty", size_bytes=0)

    try:
        with open(path, "rb") as handle:
            header = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        return StoreState(path=path, state="empty", size_bytes=size)
    if header != _SQLITE_MAGIC:
        return StoreState(path=path, state="empty", size_bytes=size)

    tables = _table_names(path)
    if not any(core in tables for core in CORE_TABLES):
        return StoreState(
            path=path, state="schemaless", tables=tables, size_bytes=size
        )
    return StoreState(path=path, state="populated", tables=tables, size_bytes=size)


__all__ = [
    "CORE_TABLES",
    "STORE_STATES",
    "StoreState",
    "inspect_store",
]
