"""SQLite-backed attempt log for ``PaneAction`` executions.

Every run of a :class:`PaneAction` (see ``action_base.py``) writes
one ``attempts`` row so the fleet accumulates structured evidence
of what was tried and how it ended. The log is **ad-hoc** — humans
and future tooling read it to debug / revise the package; there is
no runtime post-hook that mutates behavior based on the log.

Scope
-----
- One host-level DB: ``~/.scitex/agent-container/actions.db``.
- One row per attempt; ``agent`` is a column so cross-agent
  queries (e.g. "which hosts see the most ``silent`` probes?") are
  trivial.
- Snapshots (``pane_before`` / ``pane_after``) are stored as JSON
  blobs with a ``format`` discriminator so the on-disk schema can
  later accept diff-encoded variants without breaking readers.

Design rules (follows ``event_log.py`` conventions)
--------------------------------------------------
- **Non-agentic.** Pure SQL + ``json``.
- **Fail-closed.** Any write-path exception is swallowed so a
  logging hiccup never takes an agent down.
- **Stdlib only.** ``sqlite3`` + ``json`` + ``pathlib``.
- **Injectable.** ``root`` override everywhere so tests use a
  temp directory.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_ROOT = (
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".scitex") / "agent-container"
)
DEFAULT_DB_FILENAME = "actions.db"
DEFAULT_RETENTION_DAYS = int(os.environ.get("SCITEX_AGENT_ACTION_RETENTION_DAYS", "30"))
# Cap snapshot text at 4 KB per side. Bigger captures rarely carry
# more signal and the DB grows fast otherwise.
DEFAULT_SNAPSHOT_MAX_CHARS = int(
    os.environ.get("SCITEX_AGENT_ACTION_SNAPSHOT_MAX_CHARS", "4096")
)

# Canonical outcome strings. Declared here so tests and callers can
# import them without dragging in the base-class module.
OUTCOMES = (
    "success",
    "precondition_fail",
    "send_error",
    "completion_timeout",
    "skipped_by_policy",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    agent        TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    elapsed_s    REAL    NOT NULL,
    pane_before  TEXT,
    pane_after   TEXT,
    extras       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
CREATE INDEX IF NOT EXISTS idx_attempts_agent_action ON attempts(agent, action);
"""


def _safe_float(value: Any) -> float | None:
    """Coerce to float, returning None for None / unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # stx-allow: fallback (reason: type coercion or format mismatch)
        return None


def _db_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else DEFAULT_ROOT
    base.mkdir(parents=True, exist_ok=True)
    return base / DEFAULT_DB_FILENAME


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL mode and the schema ensured.

    WAL keeps concurrent reads safe while a single writer inserts.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _truncate_snapshot(
    snap: Any, max_chars: int = DEFAULT_SNAPSHOT_MAX_CHARS
) -> dict[str, Any] | None:
    """Coerce a snapshot into the ``{format, text}`` wrapper.

    Accepts:
    - None                       -> returns None
    - dict with 'format' key     -> passes through after text truncation
    - plain str                  -> wraps as {"format": "full", "text": s}
    - other dict                 -> returns {"format": "full", "text": json-dump}
    """
    if snap is None:
        return None
    if isinstance(snap, dict) and "format" in snap:
        out = dict(snap)
        text = out.get("text", "")
        if isinstance(text, str) and len(text) > max_chars:
            out["text"] = text[-max_chars:]
            out["truncated"] = True
        return out
    if isinstance(snap, str):
        s = snap[-max_chars:] if len(snap) > max_chars else snap
        return {
            "format": "full",
            "text": s,
            **({"truncated": True} if len(snap) > max_chars else {}),
        }
    # Arbitrary serializable object — dump once then truncate.
    try:
        dumped = json.dumps(snap, default=str)
    except (TypeError, ValueError):  # stx-allow: fallback (reason: type coercion or format mismatch)
        dumped = str(snap)
    s = dumped[-max_chars:] if len(dumped) > max_chars else dumped
    return {"format": "json-dump", "text": s}


def append_attempt(
    record: dict[str, Any],
    *,
    root: Path | None = None,
) -> None:
    """Insert one attempt row. Never raises.

    Required record keys:

    * ``agent`` (str)
    * ``action`` (str) — the action name
    * ``outcome`` (one of :data:`OUTCOMES`)
    * ``elapsed_s`` (float)

    Optional:

    * ``ts`` — ISO-8601 UTC; auto-populated if missing.
    * ``pane_before`` / ``pane_after`` — snapshot dicts / strings.
    * ``extras`` — action-specific fields (dict).
    """
    try:
        agent = str(record["agent"])
        action = str(record["action"])
        outcome = str(record["outcome"])
        elapsed_s = float(record["elapsed_s"])
    except (KeyError, TypeError, ValueError) as exc:  # stx-allow: fallback (reason: type coercion or format mismatch)
        logger.warning("action_store.append_attempt: invalid record: %s", exc)
        return
    ts = str(record.get("ts") or datetime.now(timezone.utc).isoformat())
    pane_before = _truncate_snapshot(record.get("pane_before"))
    pane_after = _truncate_snapshot(record.get("pane_after"))
    extras = record.get("extras") or {}
    # stx-allow: fallback (reason: DB insert can fail due to disk full, file lock, or corrupt schema; swallowing keeps agent alive per fail-closed design)
    try:
        conn = _get_conn(_db_path(root))
        try:
            conn.execute(
                "INSERT INTO attempts "
                "(ts, agent, action, outcome, elapsed_s, pane_before, pane_after, extras) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    agent,
                    action,
                    outcome,
                    elapsed_s,
                    json.dumps(pane_before) if pane_before is not None else None,
                    json.dumps(pane_after) if pane_after is not None else None,
                    json.dumps(extras),
                ),
            )
        finally:
            conn.close()
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("action_store.append_attempt: insert failed: %s", exc)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("pane_before", "pane_after", "extras"):
        raw = out.get(key)
        if raw is None:
            continue
        try:
            out[key] = json.loads(raw)
        except (TypeError, ValueError):  # stx-allow: fallback (reason: type coercion or format mismatch)
            # Leave the raw string in place; the JSON column is
            # corrupt but the rest of the row is still useful.
            pass
    return out


def _parse_since(since: str | datetime | None) -> str | None:
    """Accept either a datetime, an ISO string, or a human-readable
    relative string like ``'24h'`` / ``'7d'`` / ``'30m'`` and return
    an ISO UTC string suitable for a ``ts >= ?`` comparison."""
    if since is None:
        return None
    if isinstance(since, datetime):
        return since.astimezone(timezone.utc).isoformat()
    if isinstance(since, str):
        s = since.strip().lower()
        if not s:
            return None
        # Relative form: <number><unit>.
        if s[-1] in ("s", "m", "h", "d") and s[:-1].replace(".", "", 1).isdigit():
            n = float(s[:-1])
            unit = s[-1]
            seconds = {
                "s": n,
                "m": n * 60,
                "h": n * 3600,
                "d": n * 86400,
            }[unit]
            t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            return t.isoformat()
        # Otherwise treat as ISO.
        return since
    raise TypeError(f"Unsupported 'since' type: {type(since).__name__}")


def query(
    *,
    agent: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    since: str | datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return matching attempts, newest first."""
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    if action:
        conditions.append("action = ?")
        params.append(action)
    if outcome:
        conditions.append("outcome = ?")
        params.append(outcome)
    since_iso = _parse_since(since)
    if since_iso:
        conditions.append("ts >= ?")
        params.append(since_iso)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        "SELECT id, ts, agent, action, outcome, elapsed_s, "
        "pane_before, pane_after, extras FROM attempts"
        + where
        + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    )
    params.extend([int(limit), int(offset)])
    # stx-allow: fallback (reason: DB read can fail due to missing file, lock contention, or I/O error; empty list is a safe no-results response)
    try:
        conn = _get_conn(_db_path(root))
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("action_store.query failed: %s", exc)
        return []
    return [_row_to_dict(r) for r in rows]


def stats(
    *,
    agent: str | None = None,
    since: str | datetime | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Per-(action, outcome) counts + mean/p95 elapsed.

    Returns a list of rows::

        [{"action": "nonce-probe", "outcome": "success", "count": 42,
          "mean_elapsed_s": 3.1, "p95_elapsed_s": 5.9}, ...]

    p95 is computed in Python (sqlite has no native percentile);
    sample sets are bounded by query limits so it stays cheap.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    since_iso = _parse_since(since)
    if since_iso:
        conditions.append("ts >= ?")
        params.append(since_iso)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql_counts = (
        "SELECT action, outcome, COUNT(*) AS count, AVG(elapsed_s) AS mean "
        "FROM attempts" + where + " GROUP BY action, outcome "
        "ORDER BY action ASC, count DESC"
    )
    sql_samples = "SELECT action, outcome, elapsed_s FROM attempts" + where
    # stx-allow: fallback (reason: DB read can fail due to missing file, lock contention, or I/O error; empty list means no stats available, which is acceptable for a best-effort summary)
    try:
        conn = _get_conn(_db_path(root))
        try:
            groups = conn.execute(sql_counts, tuple(params)).fetchall()
            samples = conn.execute(sql_samples, tuple(params)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("action_store.stats failed: %s", exc)
        return []
    # Bucket samples by (action, outcome) for p95.
    buckets: dict[tuple[str, str], list[float]] = {}
    for s in samples:
        key = (s["action"], s["outcome"])
        buckets.setdefault(key, []).append(float(s["elapsed_s"]))
    out: list[dict[str, Any]] = []
    for g in groups:
        samples_sorted = sorted(buckets.get((g["action"], g["outcome"]), []))
        p95 = None
        if samples_sorted:
            idx = max(0, int(0.95 * (len(samples_sorted) - 1)))
            p95 = float(samples_sorted[idx])
        out.append(
            {
                "action": g["action"],
                "outcome": g["outcome"],
                "count": int(g["count"]),
                "mean_elapsed_s": float(g["mean"]) if g["mean"] is not None else None,
                "p95_elapsed_s": p95,
            }
        )
    return out


def summarize(
    agent: str,
    *,
    limit: int = 100,
    root: Path | None = None,
) -> dict[str, Any]:
    """Compact summary for the status payload / heartbeat.

    Keys::

        {
          "last_action_at":      ISO ts or ""
          "last_action_name":    action name or ""  (renamed from
                                                     "last_action" to avoid
                                                     collision with the
                                                     pre-existing orochi
                                                     liveness timestamp field)
          "last_action_outcome": outcome string or ""
          "last_action_elapsed_s": float or None
          "counts":              {"<action>:<outcome>": n}
          "p95_elapsed_s_by_action": {"<action>": p95 float}
        }
    """
    rows = query(agent=agent, limit=limit, root=root)
    if not rows:
        return {
            "last_action_at": "",
            "last_action_name": "",
            "last_action_outcome": "",
            "last_action_elapsed_s": None,
            "counts": {},
            "p95_elapsed_s_by_action": {},
        }
    last = rows[0]  # newest first from query()
    counts: dict[str, int] = {}
    samples_by_action: dict[str, list[float]] = {}
    for r in rows:
        key = f"{r['action']}:{r['outcome']}"
        counts[key] = counts.get(key, 0) + 1
        try:
            samples_by_action.setdefault(r["action"], []).append(float(r["elapsed_s"]))
        except (TypeError, ValueError):  # stx-allow: fallback (reason: type coercion or format mismatch)
            pass
    p95: dict[str, float] = {}
    for name, samples in samples_by_action.items():
        if not samples:
            continue
        ordered = sorted(samples)
        idx = max(0, int(0.95 * (len(ordered) - 1)))
        p95[name] = float(ordered[idx])
    return {
        "last_action_at": str(last.get("ts") or ""),
        "last_action_name": str(last.get("action") or ""),
        "last_action_outcome": str(last.get("outcome") or ""),
        "last_action_elapsed_s": _safe_float(last.get("elapsed_s")),
        "counts": counts,
        "p95_elapsed_s_by_action": p95,
    }


def purge_old(
    *,
    days: int | None = None,
    root: Path | None = None,
) -> int:
    """Delete rows older than ``days`` (default from env). Returns
    rows deleted. Safe to call periodically from a cron / daemon."""
    d = int(days) if days is not None else DEFAULT_RETENTION_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
    # stx-allow: fallback (reason: DB delete can fail due to disk error or lock; returning 0 is safe since purge is maintenance-only and a missed run leaves stale rows that will be retried next call)
    try:
        conn = _get_conn(_db_path(root))
        try:
            cur = conn.execute("DELETE FROM attempts WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount
            conn.execute("VACUUM")
            return int(deleted)
        finally:
            conn.close()
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("action_store.purge_old failed: %s", exc)
        return 0


def _all_rows(root: Path | None = None) -> Iterable[dict[str, Any]]:
    """Iterator over every row — used by tests / export tooling."""
    # stx-allow: fallback (reason: DB read can fail due to missing file or I/O error; empty iterable is safe since _all_rows is used by tests/export tooling where no results is a valid outcome)
    try:
        conn = _get_conn(_db_path(root))
        try:
            rows = conn.execute(
                "SELECT id, ts, agent, action, outcome, elapsed_s, "
                "pane_before, pane_after, extras "
                "FROM attempts ORDER BY ts ASC, id ASC"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("action_store._all_rows failed: %s", exc)
        return []
    return [_row_to_dict(r) for r in rows]
