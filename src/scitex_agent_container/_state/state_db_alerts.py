"""Structural alerts emitted by the mutual heartbeat watch (2026-06-14).

Operator mandate (lead a2a 1781e82a): agents and lead cross-monitor
each other's ``heartbeat_at`` + ``session.jsonl`` growth so a stale
peer raises a STRUCTURAL alert (not a silent drift).

A *structural* alert is the typed evidence that ``observer`` watched
``peer`` and the peer failed a freshness check. Storage shape:

  * ``observer`` — who noticed (the watching agent's name).
  * ``peer`` — who looks stale (the watched agent's name).
  * ``kind`` — alert category, e.g. ``"stale_heartbeat"`` /
    ``"stale_session_jsonl"``. Free-form short string so future
    kinds (lineage anomaly, cap-marker stuck) drop in without a
    schema change.
  * ``first_seen_at`` / ``last_seen_at`` — UNIX seconds (REAL) for
    de-duplication: re-firing the same triplet bumps ``last_seen_at``
    and the per-fire counter, NOT a fresh row, so an N-minute stale
    peer produces ONE alert, not 30 spam rows.
  * ``hit_count`` — how many beats observed the staleness. ``1`` on
    first record, ``+=1`` on every subsequent re-fire.
  * ``resolved_at`` — set by :func:`resolve_alert` when the observer
    sees the peer recover (heartbeat fresh again). NULL means the
    alert is still firing.
  * ``evidence_json`` — verbatim JSON snapshot of the staleness
    proof (age_seconds, threshold_s, peer_state_path, last hb ts,
    last session.jsonl mtime / size). Lets the lead read WHY the
    alert fired without re-running the probe.

The DDL is defined here (not in :mod:`state_db._SCHEMA_REGISTRY`)
because it is additive and lives on its own migration timeline.
:func:`state_db.init_schema` calls :func:`ensure_schema` so a fresh
``state.db`` includes it; existing DBs pick it up on next open.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# Public table name + canonical kind strings. Kept here so the test
# suite + CLI consumers (``sac db query --table=structural_alerts``)
# import one constant rather than re-typing the literal.
TABLE_NAME = "structural_alerts"
KIND_STALE_HEARTBEAT = "stale_heartbeat"
KIND_STALE_SESSION_JSONL = "stale_session_jsonl"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    alert_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    observer        TEXT NOT NULL,
    peer            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 1,
    resolved_at     REAL,
    evidence_json   TEXT,
    UNIQUE (observer, peer, kind, resolved_at)
);
CREATE INDEX IF NOT EXISTS idx_structural_alerts_active
    ON {TABLE_NAME}(observer, peer, kind) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_structural_alerts_peer
    ON {TABLE_NAME}(peer);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``structural_alerts`` table + indexes.

    Called from :func:`state_db.init_schema` so a fresh ``state.db``
    carries the table without a separate migration step. Safe to call
    against an already-initialised DB — every statement is ``IF NOT
    EXISTS`` guarded.
    """
    conn.executescript(_SCHEMA)


def record_alert(
    *,
    observer: str,
    peer: str,
    kind: str,
    evidence: dict | None = None,
    now: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Upsert one structural alert. Returns the row's ``alert_id``.

    Deduplication: the active row for ``(observer, peer, kind)`` —
    i.e. ``resolved_at IS NULL`` — is bumped (``last_seen_at`` +
    ``hit_count``) instead of inserting a fresh one. This prevents
    an N-minute stale peer from producing 30 noise rows.

    ``evidence`` is JSON-encoded and stamped onto the new/updated row
    so the lead can read WHY the alert fired (age vs threshold, last
    hb timestamp, peer_state_path) without re-running the probe.
    """
    from .state_db import open_db

    ts = float(now) if now is not None else time.time()
    payload = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)
    with open_db(db_path) as conn:
        ensure_schema(conn)
        row = conn.execute(
            f"SELECT alert_id, hit_count FROM {TABLE_NAME} "
            "WHERE observer=? AND peer=? AND kind=? AND resolved_at IS NULL",
            (observer, peer, kind),
        ).fetchone()
        if row is not None:
            new_count = int(row["hit_count"]) + 1
            conn.execute(
                f"UPDATE {TABLE_NAME} SET last_seen_at=?, hit_count=?, "
                "evidence_json=? WHERE alert_id=?",
                (ts, new_count, payload, row["alert_id"]),
            )
            return int(row["alert_id"])
        cur = conn.execute(
            f"INSERT INTO {TABLE_NAME} "
            "(observer, peer, kind, first_seen_at, last_seen_at, "
            "hit_count, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (observer, peer, kind, ts, ts, payload),
        )
        return int(cur.lastrowid or 0)


def resolve_alert(
    *,
    observer: str,
    peer: str,
    kind: str,
    now: float | None = None,
    db_path: Path | None = None,
) -> bool:
    """Mark the active alert for ``(observer, peer, kind)`` resolved.

    Returns True iff an active row existed and was updated. Idempotent:
    resolving an already-resolved or never-fired triplet is a no-op
    returning False — callers can splat this on every healthy beat
    without checking first.
    """
    from .state_db import open_db

    ts = float(now) if now is not None else time.time()
    with open_db(db_path) as conn:
        ensure_schema(conn)
        cur = conn.execute(
            f"UPDATE {TABLE_NAME} SET resolved_at=? "
            "WHERE observer=? AND peer=? AND kind=? AND resolved_at IS NULL",
            (ts, observer, peer, kind),
        )
        return cur.rowcount > 0


def list_active_alerts(
    *,
    observer: str | None = None,
    peer: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return every unresolved structural alert, optionally filtered.

    Empty list when nothing has fired — the lead reads this as the
    cross-monitor heartbeat is clean. Rows include the verbatim
    ``evidence_json`` blob so the consumer can render the staleness
    proof without a second query.
    """
    from .state_db import open_db

    clauses: list[str] = ["resolved_at IS NULL"]
    params: list[object] = []
    if observer is not None:
        clauses.append("observer=?")
        params.append(observer)
    if peer is not None:
        clauses.append("peer=?")
        params.append(peer)
    sql = (
        f"SELECT * FROM {TABLE_NAME} WHERE "
        + " AND ".join(clauses)
        + " ORDER BY last_seen_at DESC"
    )
    with open_db(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


__all__ = [
    "KIND_STALE_HEARTBEAT",
    "KIND_STALE_SESSION_JSONL",
    "TABLE_NAME",
    "ensure_schema",
    "list_active_alerts",
    "record_alert",
    "resolve_alert",
]
