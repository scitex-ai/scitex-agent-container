"""Diary-style writes to state.db (2026-05-17).

Three tables let every agent write a journal that the lead reads
and filters:

  * ``turns`` — one row per state-transition of a /v1/turn flow
    (``queued``, ``delivered``, ``read``, ``responded``, ``error``).
    A successful turn produces four rows sharing a single ``turn_id``.
  * ``errors`` — one row per caught error (auth, network, sdk-crash,
    schema-mismatch, ...). Optionally tied to a ``turn_id``.
  * ``heartbeats`` — promotes the per-agent ``heartbeat.json`` file
    into a queryable table; one row per heartbeat tick.

DDL lives in :mod:`state_db`; this module owns the WRITE + READ
helpers so :mod:`state_db` can stay under the per-file line cap.

All times are stored as ``REAL`` unix-seconds (float). The lead can
mix them with the legacy ``TEXT NOT NULL`` ISO-8601 columns on
``instances`` because both formats sort consistently within their
own table; cross-table joins are time-window queries (BETWEEN),
not joins on ts equality.
"""

from __future__ import annotations

import time
from pathlib import Path

# Bounds on the optional inline-prompt/response and error-detail
# columns so a runaway message can't bloat state.db. Picked to
# match the user's "first ~500 chars / first ~1000 chars" spec.
_TURN_TEXT_LIMIT = 500
_ERROR_DETAIL_LIMIT = 1000


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def record_turn(
    *,
    turn_id: str,
    name: str,
    host: str,
    status: str,
    prompt_text: str | None = None,
    response_text: str | None = None,
    session_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    ts: float | None = None,
    db_path: Path | None = None,
) -> None:
    """Append one ``turns`` row.

    ``turn_id`` is the lead-assigned uuid that ties the four
    state-transition rows of a single conversation turn together.
    Each call adds a row — the table is intentionally append-only
    so the timeline of state transitions stays auditable.
    """
    from .state_db import open_db

    row_ts = float(ts) if ts is not None else time.time()
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO turns (
                turn_id, name, host, status,
                prompt_text, response_text, ts,
                session_id, input_tokens, output_tokens
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                name,
                host,
                status,
                _clip(prompt_text, _TURN_TEXT_LIMIT),
                _clip(response_text, _TURN_TEXT_LIMIT),
                row_ts,
                session_id,
                input_tokens,
                output_tokens,
            ),
        )


def record_error(
    *,
    name: str,
    host: str,
    cause: str,
    detail: str | None = None,
    turn_id: str | None = None,
    ts: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Append one ``errors`` row. Returns the new ``error_id``.

    ``cause`` is a short identifier the lead can group on (auth /
    network / sdk-crash / schema-mismatch / ...); ``detail`` carries
    the longer message or traceback (truncated to 1000 chars).
    """
    from .state_db import open_db

    row_ts = float(ts) if ts is not None else time.time()
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO errors (name, host, cause, detail, ts, turn_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                host,
                cause,
                _clip(detail, _ERROR_DETAIL_LIMIT),
                row_ts,
                turn_id,
            ),
        )
        return int(cur.lastrowid or 0)


def record_heartbeat(
    *,
    name: str,
    host: str,
    pid: int | None,
    state: str,
    ts: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Append one ``heartbeats`` row. Returns the new ``heartbeat_id``.

    Mirrors the legacy ``heartbeat.json`` payload (pid + state) but
    in a cross-host queryable table. ``state`` follows the runner's
    state-machine vocabulary (``starting`` | ``idle`` | ``working``
    | ``stopping`` | ``error`` | ``down``).
    """
    from .state_db import open_db

    row_ts = float(ts) if ts is not None else time.time()
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO heartbeats (name, host, pid, state, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, host, pid, state, row_ts),
        )
        return int(cur.lastrowid or 0)


def latest_heartbeats_per_name(db_path: Path | None = None) -> list[dict]:
    """Return one heartbeat row per agent ``name`` — the most recent.

    Used by ``sac db query --table=heartbeats --latest`` and by the
    lead when it wants the current state-of-the-fleet snapshot
    without paging through every historical beat.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT h.*
              FROM heartbeats h
              JOIN (
                    SELECT name, MAX(ts) AS max_ts
                      FROM heartbeats
                  GROUP BY name
                   ) latest
                ON h.name = latest.name AND h.ts = latest.max_ts
              ORDER BY h.name
            """
        ).fetchall()
        return [dict(r) for r in rows]


__all__ = [
    "record_turn",
    "record_error",
    "record_heartbeat",
    "latest_heartbeats_per_name",
]
