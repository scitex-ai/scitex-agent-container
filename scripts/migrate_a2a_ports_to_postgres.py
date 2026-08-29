#!/usr/bin/env python3
"""One-shot: copy the SQLite ``a2a_ports`` table into PostgreSQL.

Companion to the ``port_allocator`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so live claims are not
stranded in a file nothing opens any more.

AN UNMIGRATED CLAIM IS NOT A LOST OBSERVATION, IT IS A COLLISION
================================================================
The diary held history: losing a heartbeat row costs a reading. A row here
says "agent X is BOUND to port N right now". Lose it and two things break, and
the second is the serious one:

  * ``sac agents list`` / ``sac listen`` forwarding stop finding the port for
    every agent that is already running, and degrade to the ``instances``-row
    fallback — a visible, recoverable loss;
  * the allocator's scan sees the port as FREE and hands it to the NEXT agent
    that starts, whose runner then loses the bind to the process already
    sitting on it. That is precisely the collision ``UNIQUE(port)`` existed to
    prevent, arriving by a different door.

So run this BEFORE the restart that picks up the new code, not after, and read
the verify line rather than the exit code.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would migrate
an empty shard and report success — the same shape that let the state-write
outage look finished for four days.

THE IDENTITY INVERTS, AND THAT IS SAFE HERE
===========================================
SQLite keyed on ``name TEXT PRIMARY KEY`` with a separate ``UNIQUE(port)``.
The store keys on ``port``, because that is the invariant worth carrying
structurally (see ``_state/port_allocator_store``). The inversion is lossless
only because SQLite already enforced one row per port — so a table that
satisfied its own constraint cannot produce two claims on one port here. If
this script ever reports a duplicate port it means the source table was
already corrupt, and it says so rather than silently keeping one.

``claimed_at`` CHANGES TYPE, AND A BAD ONE IS REPORTED, NOT DROPPED
===================================================================
The column was ISO-8601 TEXT and is epoch REAL in the store. A value that will
not parse must NOT cost the claim: the timestamp is cosmetic (only
``sac ports --json`` renders it) while the claim is load-bearing. Such rows
migrate with ``claimed_at=0.0`` and the count is PRINTED, so a reader sees it
happened instead of discovering it later.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state.port_allocator_store import (  # noqa: E402
    STORE_NAME,
    open_port_store,
)

TABLE = "a2a_ports"
COLUMNS = ("name", "port", "claimed_at")


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _epoch(raw: object) -> float | None:
    """The ISO text as epoch seconds, or ``None`` when it will not parse."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _read_rows(db_path: Path) -> list[dict]:
    """Every claim in the SQLite table, ascending by port."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        names = {r["name"] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
        if not names:
            return []
        cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY port")
        return [{c: r[c] for c in COLUMNS} for r in cur.fetchall()]
    finally:
        conn.close()


def _duplicate_ports(rows: list[dict]) -> list[int]:
    """Ports claimed more than once in the SOURCE — a corrupt table, reported."""
    seen: set[int] = set()
    dupes: list[int] = []
    for row in rows:
        port = int(row["port"])
        if port in seen:
            dupes.append(port)
        seen.add(port)
    return dupes


def _migrate(rows: list[dict], commit: bool) -> tuple[int, int, int]:
    """Returns ``(written, already_present, undated)``.

    The store field is ``claimed_by`` (the claim protocol settled on PR
    #1243's review), while the SQLite column stays ``name``; the rename
    happens here so a claim written by this script is indistinguishable
    from one written by ``claim_port``.

    A record that is PRESENT but carries no ``claimed_by`` is REPAIRED
    rather than skipped: PR #1243's own end-to-end exercise wrote rows
    under the pre-review field name (``name``), and the additive column
    migration leaves ``claimed_by`` NULL on those — a port that reads as
    held by nobody nameable. Re-putting the SQLite values fills it in.
    """
    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    undated = sum(1 for row in rows if _epoch(row["claimed_at"]) is None)
    if not commit:
        return (len(rows), 0, undated)

    written = present = 0
    store = open_port_store()
    try:
        for row in rows:
            key = {"port": int(row["port"])}
            claimed_at = _epoch(row["claimed_at"])
            values = {
                "port": int(row["port"]),
                "claimed_by": str(row["name"]),
                "claimed_at": 0.0 if claimed_at is None else claimed_at,
            }
            current = store.get(key, include_hidden=True)
            if current is not None:
                if current.values.get("claimed_by") is None:
                    store.put(values, expected_revision=ANY_REVISION)
                    written += 1
                else:
                    present += 1
                continue
            try:
                store.put(values, expected_revision=NEW_RECORD)
                written += 1
            except RevisionMismatchError:
                # Another writer got there between the read and the put. Not
                # an error: the record exists, which is the goal.
                present += 1
    finally:
        store.close()
    return (written, present, undated)


def _verify() -> int:
    """Count what is actually IN the store, by reading it back."""
    store = open_port_store()
    try:
        return len(store.rows(include_hidden=True))
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite state.db to read from (default: the host's)",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")
    print(f"source: {db_path}")
    print(f"target: {STORE_NAME}")

    rows = _read_rows(db_path)
    dupes = _duplicate_ports(rows)
    if dupes:
        print(f"REFUSING: port(s) claimed twice in the SOURCE table: {sorted(dupes)}")
        print("The SQLite UNIQUE(port) constraint cannot have allowed this, so the")
        print("table is corrupt. Fix it there — do not let this script pick a winner.")
        return 1

    if not rows:
        print(f"{TABLE}: 0 rows in SQLite — nothing to move")
        if args.commit:
            print(f"verify: {_verify()} row(s) in the store")
        return 0

    written, present, undated = _migrate(rows, args.commit)
    if undated:
        print(f"{TABLE}: {undated} row(s) carry an unparseable claimed_at -> 0.0")
    if args.commit:
        print(f"{TABLE}: {written} written, {present} already present")
        print(f"verify: {_verify()} row(s) in the store (read back)")
    else:
        print(f"{TABLE}: {written} rows WOULD move (pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
