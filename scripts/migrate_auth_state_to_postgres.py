#!/usr/bin/env python3
"""One-shot: copy the SQLite ``agent_auth_state`` rows into per-host PostgreSQL.

Companion to the ``auth_state`` port. The code stopped reading SQLite; this
moves the rows already there so the cache is not cold on the first
``sac agents list`` after the switch.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write. The bare
invocation reports what would move and touches nothing. (Its sibling
``migrate_incarnations_to_postgres.py`` took ``--dry-run`` as the opt-in until
2026-08-24, which made the obvious invocation the destructive one; that is
fixed and this one is built the safe way from the start.)

RUN IT ON THE HOST, NOT IN A CONTAINER. ``DEFAULT_DB_PATH`` resolves
differently in the two places: a container gets its own per-agent shard under
``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. The rows are on the HOST, so a
container run would faithfully migrate an empty shard and report success.

RUN IT ONCE, BEFORE restarting the writers — not after. ``checked_at`` is
LAST_WRITER_WINS decided by the WRITE's clock, not by the value. A straggler
pass run after the new code is live would stamp an OLD verdict over a FRESH
one, including ``auth_failed=True`` for an agent that has since recovered.
That is the backfilled-timestamp hazard running in the destructive direction,
in the one place where the timestamp IS the safety mechanism.

IT IS IDEMPOTENT and it VERIFIES: each record is read back through the same
production path, and a mismatch is reported rather than counted as success.
Nothing is deleted from SQLite — the old table stays untouched as a fallback.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_COLUMNS = ("name", "auth_failed", "checked_at", "banner", "reason", "note")


def _sqlite_rows(db_path: Path) -> list[dict]:
    """Every agent_auth_state row, read-only. Empty when the table is gone."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = ", ".join(_COLUMNS)
        return [dict(r) for r in conn.execute(f"SELECT {cols} FROM agent_auth_state")]
    except sqlite3.OperationalError as exc:
        print(f"  no agent_auth_state table in {db_path}: {exc}")
        return []
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None,
                    help="SQLite state.db to read (default: sac's resolved DEFAULT_DB_PATH)")
    ap.add_argument("--commit", action="store_true",
                    help="actually write; without it this is a dry run that writes nothing")
    args = ap.parse_args()

    from scitex_agent_container._state.auth_state import (
        auth_state_store_target,
        get_auth_state,
        record_auth_checks,
    )
    from scitex_agent_container._state.state_db import DEFAULT_DB_PATH

    db_path = args.db or DEFAULT_DB_PATH
    print(f"source (sqlite)  : {db_path}")
    print(f"target (postgres): {auth_state_store_target().locator}")

    if not Path(db_path).exists():
        print("source does not exist — nothing to migrate")
        return 0

    rows = _sqlite_rows(Path(db_path))
    print(f"rows in sqlite   : {len(rows)}")
    if not rows:
        return 0

    if not args.commit:
        print("dry run (no --commit): writing nothing")
        for r in rows[:20]:
            print(f"  would write  {r['name']:<34} auth_failed={r['auth_failed']} "
                  f"checked_at={r['checked_at']}")
        print("  re-run with --commit to apply")
        return 0

    # Each row carries its OWN checked_at. Writing them one at a time preserves
    # every recorded stamp verbatim; a single batch with one `checked_at` would
    # rewrite all twelve to the migration's own clock, which is exactly the
    # "backfilled timestamp makes old things look new" failure.
    written = 0
    for r in rows:
        written += record_auth_checks(
            [{"name": r["name"], "auth_failed": bool(r["auth_failed"]),
              "banner": r["banner"], "reason": r["reason"] or "",
              "note": r["note"] or ""}],
            checked_at=r["checked_at"],
        )
    print(f"written          : {written}")

    # VERIFY through the production read path, not the write handle.
    missing, restamped = [], []
    for r in rows:
        got = get_auth_state(r["name"])
        if got is None:
            missing.append(r["name"])
        elif got["checked_at"] != r["checked_at"]:
            restamped.append((r["name"], r["checked_at"], got["checked_at"]))
    if missing:
        print(f"MISSING after write ({len(missing)}): {missing[:10]}")
    if restamped:
        print(f"RESTAMPED ({len(restamped)}): {restamped[:5]}")
    if missing or restamped:
        return 1
    print(f"verified         : {len(rows)}/{len(rows)} readable with original stamps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
