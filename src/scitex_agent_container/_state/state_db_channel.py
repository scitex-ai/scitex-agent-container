"""Channel-event persistence + replay (WI-1, handoff §4).

The in-memory :class:`Broker` in :mod:`a2a._inbox_bus` is what
``message:send`` publishes to and what ``/agents/<name>/inbox/stream``
consumes — but it has no persistence, so an event POSTed while no
SSE subscriber is connected is lost forever. This module fills that
gap.

Three primitives, each operating on the ``channel_events`` table in
``state.db`` (schema in :mod:`state_db`):

  * :func:`persist_event` — write one row when the bus publishes.
    Returns the row id so callers can attach it to the SSE ``id:``
    line (Last-Event-ID cursor) and later :func:`mark_delivered`.

  * :func:`list_undelivered` — fresh-subscriber replay. Yields every
    event for ``target`` whose ``delivered_at IS NULL`` in
    monotonic-id order. The acceptance criterion in the handoff is:
    "an event POSTed with no subscriber is delivered on connect" —
    that is exactly this list.

  * :func:`list_since_id` — Last-Event-ID replay. Yields every event
    for ``target`` whose ``id > since_id``, regardless of
    ``delivered_at``. A reconnecting client that already saw
    ``Last-Event-ID = N`` resumes at ``N+1``.

  * :func:`mark_delivered` — set ``delivered_at`` on a batch of row
    ids. Idempotent (we only update rows where
    ``delivered_at IS NULL``); subsequent calls are no-ops.

All times stored as ``REAL`` unix-seconds (float). Matches the diary
tables (turns / errors / heartbeats) so cross-table time-window
queries work without format conversion.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "format_ts_iso",
    "persist_event",
    "list_undelivered",
    "list_since_id",
    "mark_delivered",
]


def format_ts_iso(ts: object) -> str:
    """Render a stored channel-event ``ts`` as an ISO-8601 UTC string.

    Display formatter for emitters that surface a ``ts`` from the
    ``channel_events`` table or a minted bus envelope (where ``ts`` is
    unix-seconds, see :func:`persist_event` and
    :func:`a2a._inbox_bus.mint_event`). On-disk storage stays
    unix-seconds (``channel_events.ts REAL``); ONLY the rendered /
    emitted form is ISO-8601, matching the legacy
    :func:`state_db.now_iso` wire shape (trailing ``Z`` for UTC).

    Used by the channel-push notification path
    (:mod:`scitex_agent_container._mcp.channel`) so a receiving Claude
    session sees ``<channel ts="2026-04-21T09:30:00Z" ...>`` instead of
    the raw ``"1777766006.95"`` that was previously stringified
    verbatim. A canonical helper here keeps every channel-push display
    caller on one formatter (no per-call ``strftime`` duplication).

    Numeric input is rendered via
    ``datetime.fromtimestamp(ts, tz=utc).strftime("%Y-%m-%dT%H:%M:%SZ")``.
    A string that already parses as ISO-8601 is round-tripped verbatim
    so a double-format does not corrupt an upstream ISO value. A
    numeric-looking string (the common JSON round-trip case) is coerced
    to float and re-rendered. Anything else falls through to ``str(ts)``
    — defensive: a display helper must never raise; the on-disk column
    is the source of truth and a malformed display value would only
    obscure the underlying data.

    Empty / missing input (``""`` or ``None``) renders as ``""`` — the
    receive-side notification meta passes ``event.get("ts", "")``, and
    an empty stays empty (no ``1970-01-01T00:00:00Z`` surprise).
    """
    if ts is None:
        return ""
    if isinstance(ts, bool):
        # bool is a subclass of int — guard explicitly so a stray
        # ``True`` does not get rendered as a 1970 epoch timestamp.
        return str(ts)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return ""
        # Tolerate an already-ISO string (with or without trailing 'Z')
        # so render-helper composition never loses the timezone.
        candidate = s[:-1] if s.endswith("Z") else s
        try:
            datetime.fromisoformat(candidate)
            return s
        except ValueError:
            pass
        # Common path: JSON round-trip of a float ts arrives as a string
        # like ``"1777766006.95"``. Coerce and re-render.
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (TypeError, ValueError):
            return s
    return str(ts)


def persist_event(
    *,
    target: str,
    event: dict[str, Any],
    db_path: Path | None = None,
) -> int:
    """Insert one row into ``channel_events`` and return its row id.

    The row id is the value the SSE handler stamps onto its ``id:``
    line; the client echoes it back as ``Last-Event-ID`` to resume
    after a disconnect.

    ``event`` is the minted envelope produced by
    :func:`a2a._inbox_bus.mint_event` — stored verbatim in
    ``meta_json`` so the replay path yields byte-identical frames.
    """
    from .state_db import open_db

    source = event.get("from_agent")
    kind = event.get("kind") or "message"
    content = event.get("content")
    ts = float(event.get("ts") or time.time())
    meta_json = json.dumps(event, ensure_ascii=False)

    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO channel_events (
                target, source, kind, content, meta_json, ts
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (target, source, kind, content, meta_json, ts),
        )
        row_id = int(cur.lastrowid or 0)
    if row_id <= 0:
        # Loud failure (handoff §0 Hard rules): if SQLite did not
        # mint a row id, something is structurally wrong — surface
        # it instead of returning a sentinel that callers might
        # confuse with a real cursor.
        raise RuntimeError(
            "channel_events INSERT did not yield a row id — state.db schema mismatch?"
        )
    return row_id


def list_undelivered(
    *,
    target: str,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every undelivered event for ``target`` in id order.

    Each dict carries ``{"id", "event"}``: ``id`` is the SSE cursor,
    ``event`` is the round-tripped envelope.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, meta_json FROM channel_events
             WHERE target = ? AND delivered_at IS NULL
             ORDER BY id ASC
            """,
            (target,),
        )
        return [
            {"id": int(r["id"]), "event": json.loads(r["meta_json"])}
            for r in cur.fetchall()
        ]


def list_since_id(
    *,
    target: str,
    since_id: int,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every event for ``target`` whose id is strictly greater
    than ``since_id``, in id order.

    Used by the ``Last-Event-ID`` SSE reconnect path: the client
    passes the highest id it has *already seen* so it resumes at the
    next event without re-receiving the cursor itself.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, meta_json FROM channel_events
             WHERE target = ? AND id > ?
             ORDER BY id ASC
            """,
            (target, int(since_id)),
        )
        return [
            {"id": int(r["id"]), "event": json.loads(r["meta_json"])}
            for r in cur.fetchall()
        ]


def mark_delivered(
    row_ids: Iterable[int],
    *,
    db_path: Path | None = None,
) -> None:
    """Set ``delivered_at = now`` on every row whose id is in
    ``row_ids`` AND whose ``delivered_at`` is still NULL.

    Idempotent — calling twice does not move the timestamp. The
    ``delivered_at IS NULL`` predicate keeps the "first-delivery"
    semantics the handoff calls out ("Mark delivered exactly once
    (delivered_at)").
    """
    ids = [int(i) for i in row_ids]
    if not ids:
        return
    now = time.time()
    from .state_db import open_db

    placeholders = ",".join("?" for _ in ids)
    with open_db(db_path) as conn:
        conn.execute(
            (
                "UPDATE channel_events SET delivered_at = ? "
                f"WHERE id IN ({placeholders}) AND delivered_at IS NULL"
            ),
            (now, *ids),
        )
