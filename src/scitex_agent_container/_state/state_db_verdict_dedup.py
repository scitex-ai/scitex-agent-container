"""CI-verdict delivery dedup — the "delivered-set" (sac #404).

feedback.pdf §3: sac polls GitHub CI on its OWN schedule and a2a-delivers
each verdict to the pusher EXACTLY ONCE — deduping on
``(repo, pr, head_sha, conclusion)`` in sac's own state so a re-poll (or a
``sac listen`` restart) never re-delivers a verdict the agent already saw.
This module owns that delivered-set table in ``state.db``.

A re-run that flips the conclusion (red→green on the same head_sha) is a
DISTINCT key, so the flipped verdict IS delivered — the dedup is per exact
outcome, not per PR. A new push (new ``head_sha``) is likewise distinct.

Sibling-module pattern (mirrors :mod:`dispatch_ledger`): own
``CREATE TABLE IF NOT EXISTS`` behind :func:`init_verdict_dedup_schema`,
layered on the shared connection from :func:`state_db.open_db`, and a
lazy :func:`_ensure_table` so callers never need an explicit init. Kept
out of ``state_db.py`` so this branch never collides with concurrent
``state_db.py`` work (same rationale as the dispatch ledger).

All times are ``REAL`` unix-seconds (float), matching the diary tables.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict_delivered (
    repo          TEXT NOT NULL,
    pr            INTEGER NOT NULL,
    head_sha      TEXT NOT NULL,
    conclusion    TEXT NOT NULL,
    dispatch_id   TEXT,
    delivered_at  REAL NOT NULL,
    PRIMARY KEY (repo, pr, head_sha, conclusion)
);
"""


def init_verdict_dedup_schema(db_path: Path | None = None) -> Path:
    """Create the ``verdict_delivered`` table if missing. Idempotent.

    Delegates the base schema + connection config to
    :func:`state_db.open_db`, then layers our own
    ``CREATE TABLE IF NOT EXISTS`` so the delivered-set lives in the same
    ``state.db`` file as the registry + diary tables. Returns the
    resolved database path.
    """
    from .state_db import init_schema, open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)
    return init_schema(db_path)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Ensure the delivered-set table exists on an already-open connection."""
    conn.executescript(_SCHEMA)


def verdict_already_delivered(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    conclusion: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff this exact verdict was already delivered.

    The dedup key is the 4-tuple ``(repo, pr, head_sha, conclusion)``. A
    miss — or a brand-new ``state.db`` — returns ``False`` so the caller
    delivers. Never raises on a fresh db (the table is ensured first).
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT 1 FROM verdict_delivered "
            "WHERE repo=? AND pr=? AND head_sha=? AND conclusion=?",
            (repo, int(pr), head_sha, conclusion),
        ).fetchone()
    return row is not None


def record_verdict_delivered(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    conclusion: str,
    dispatch_id: str | None = None,
    delivered_at: float | None = None,
    db_path: Path | None = None,
) -> None:
    """Mark this verdict delivered. Idempotent (re-poll-safe).

    Uses ``INSERT OR IGNORE`` on the 4-tuple primary key so re-seeing the
    same verdict is a no-op rather than an ``IntegrityError`` — the loop
    can record unconditionally after a successful delivery.
    """
    from .state_db import open_db

    ts = float(delivered_at) if delivered_at is not None else time.time()
    with open_db(db_path) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO verdict_delivered "
            "(repo, pr, head_sha, conclusion, dispatch_id, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo, int(pr), head_sha, conclusion, dispatch_id, ts),
        )


def failures_since_last_success(
    *,
    repo: str,
    pr: int,
    db_path: Path | None = None,
) -> int:
    """Count failure verdicts delivered for this PR since its last green.

    The dedup key includes ``head_sha``, so a PR whose head keeps moving
    re-fires forever — every push is a fresh key. That is correct for a
    branch someone is pushing fixes to, and wrong for a standing sync PR
    whose head tracks its source branch: there, each unrelated merge
    moves the head and earns another "fix-and-push" the recipient cannot
    act on. This count is the streak the caller caps on.

    Counts only rows *newer* than the most recent ``success`` for the
    same ``(repo, pr)``, so a red→green→red sequence starts over rather
    than staying capped forever. With no success on record the floor is
    ``0.0``, which every real ``delivered_at`` exceeds.

    Returns 0 on a fresh db (the table is ensured first).
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT count(*) FROM verdict_delivered "
            "WHERE repo=? AND pr=? AND conclusion='failure' "
            "  AND delivered_at > COALESCE(("
            "        SELECT max(delivered_at) FROM verdict_delivered "
            "        WHERE repo=? AND pr=? AND conclusion='success'"
            "      ), 0.0)",
            (repo, int(pr), repo, int(pr)),
        ).fetchone()
    return int(row[0]) if row else 0


__all__ = [
    "failures_since_last_success",
    "init_verdict_dedup_schema",
    "record_verdict_delivered",
    "verdict_already_delivered",
]
