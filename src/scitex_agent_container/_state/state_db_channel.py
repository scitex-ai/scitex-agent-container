"""Channel-event persistence + replay (WI-1, handoff §4).

The in-memory :class:`Broker` in :mod:`a2a._inbox_bus` is what
``message:send`` publishes to and what ``/agents/<name>/inbox/stream``
consumes — but it has no persistence, so an event POSTed while no SSE
subscriber is connected is lost forever. This module fills that gap.

Five primitives, each operating on the ``sac_channel_events`` table (schema
and connection in :mod:`.state_db_channel_store`):

  * :func:`persist_event` — write one row when the bus publishes. Returns
    the row id so callers can attach it to the SSE ``id:`` line
    (Last-Event-ID cursor) and later :func:`mark_delivered`.

  * :func:`list_undelivered` — fresh-subscriber replay. Yields every event
    for ``target`` whose ``delivered_at IS NULL`` in monotonic-id order.

  * :func:`list_since_id` — Last-Event-ID replay. Yields every event for
    ``target`` whose ``id > since_id``, regardless of ``delivered_at``. A
    reconnecting client that already saw ``Last-Event-ID = N`` resumes at the
    next event.

  * :func:`mark_delivered` — set ``delivered_at`` on a batch of ids FOR ONE
    TARGET. Idempotent (only rows where ``delivered_at IS NULL`` are
    touched).

  * :func:`rename_channel_events` — carry an agent's whole message history
    across a rename, reversibly.

ON POSTGRESQL SINCE 2026-08-28, AND ``db_path`` IS GONE
=======================================================
``channel_events`` was the LAST table sac kept in a per-agent file.
Operator ruling, restated repeatedly through August 2026: 「スクライトなんて
全部絶滅させてください」 — because the fleet is MULTI-HOST and a state file
per host means a different truth per host. Measured on scitex-compute-04,
2026-08-12: the
in-container ``state.db`` held 0 channel events while the bare host's held
1872, both readable, every call exit 0. An empty read cannot distinguish "no
events" from "I looked in the wrong file".

``db_path`` is gone from every signature here. It named a file; there
is no file. Test isolation comes from pointing ``SCITEX_STORE_DSN`` at a
throwaway schema (the ``pg_schema`` fixture), which is stronger than a temp
path was because it exercises the real resolver.

The tables are PLAIN sac-owned PostgreSQL, not ``scitex_dev.store`` records
— three measured disqualifiers and the exit criterion that would reverse the
decision are in ``docs/adr/0023-channel-events-plain-postgres.md`` and
summarised in :mod:`.state_db_channel_store`.

FOUR CONTRACTS THIS MODULE MUST NOT BREAK
=========================================
1. ``id`` is PER-TARGET, monotonic and strictly increasing. GAPS ARE FINE:
   every reader uses ``id > cursor``, never ``id = cursor + 1``, so a
   skipped number costs nothing and a REORDERED one costs a dropped frame.
2. The id is minted SERVER-SIDE and returned SYNCHRONOUSLY. Every writer
   attaches it to the envelope as ``_row_id`` and the SSE handler stamps it
   as the ``id:`` line, so an allocation that yields nothing must RAISE
   rather than return a sentinel a caller could mistake for a cursor.
3. ``delivered_at`` is FIRST-WRITE-ONLY — the ``AND delivered_at IS NULL``
   predicate. A second delivery must not move the timestamp.
4. ``meta_json`` round-trips BYTE-IDENTICALLY. See
   :mod:`.state_db_channel_store` for why the column is ``TEXT`` and never
   passes through a JSON codec.

All times stored as double-precision unix-seconds (float). This module owns
``sac_channel_events`` and nothing else — it does NOT read the diary trio
(turns / errors / heartbeats), which named this format back when the two
shared one file. The shared wire format is kept anyway, because comparing
two timestamps across the two stores should not need a conversion.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from .state_db_channel_store import run_with_reconnect

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

__all__ = [
    "ChannelRename",
    "format_ts_iso",
    "list_since_id",
    "list_undelivered",
    "mark_delivered",
    "persist_event",
    "rename_channel_events",
    "undo_rename_channel_events",
]


def format_ts_iso(ts: object) -> str:
    """Render a stored channel-event ``ts`` as an ISO-8601 UTC string.

    Display formatter for emitters that surface a ``ts`` from the
    ``sac_channel_events`` table or a minted bus envelope (where ``ts`` is
    unix-seconds, see :func:`persist_event` and
    :func:`a2a._inbox_bus.mint_event`). Stored form stays unix-seconds
    (``ts DOUBLE PRECISION``); ONLY the rendered / emitted form is ISO-8601,
    matching the legacy ``now_iso`` wire shape (trailing ``Z`` for UTC).

    Used by the channel-push notification path
    (:mod:`scitex_agent_container._mcp.channel`) so a receiving Claude
    session sees ``<channel ts="2026-04-21T09:30:00Z" ...>`` instead of the
    raw ``"1777766006.95"`` that was previously stringified verbatim. A
    canonical helper here keeps every channel-push display caller on one
    formatter (no per-call ``strftime`` duplication).

    Numeric input is rendered via
    ``datetime.fromtimestamp(ts, tz=utc).strftime("%Y-%m-%dT%H:%M:%SZ")``.
    A string that already parses as ISO-8601 is round-tripped verbatim so a
    double-format does not corrupt an upstream ISO value. A numeric-looking
    string (the common JSON round-trip case) is coerced to float and
    re-rendered. Anything else falls through to ``str(ts)`` — defensive: a
    display helper must never raise; the stored column is the source of truth
    and a malformed display value would only obscure the underlying data.

    Empty / missing input (``""`` or ``None``) renders as ``""`` — the
    receive-side notification meta passes ``event.get("ts", "")``, and an
    empty stays empty (no ``1970-01-01T00:00:00Z`` surprise).
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


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------

#: Allocate this target's next id and RETURN it, in the caller's transaction.
#:
#: A COUNTER ROW, NOT A SEQUENCE, and the difference is a correctness one.
#: ``nextval`` is non-transactional: two concurrent writers on one target can
#: commit id N+1 before N, and a reader doing ``id > cursor ORDER BY id``
#: then ships N+1, advances past it, and NEVER RETURNS N — a silent drop
#: a single serialised writer could not produce. The ``DO UPDATE`` here takes
#: a row lock on ``(target)`` held until commit, so for one target commit
#: order IS id order. Different targets touch different rows and never
#: contend.
_ALLOCATE_SQL = """
INSERT INTO sac_channel_cursor (target, next_id)
VALUES (%s, 1)
ON CONFLICT (target) DO UPDATE SET next_id = sac_channel_cursor.next_id + 1
RETURNING next_id
"""

_INSERT_SQL = """
INSERT INTO sac_channel_events (
    target, id, source, kind, content, meta_json, ts
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def persist_event(*, target: str, event: dict[str, Any]) -> int:
    """Insert one row into ``sac_channel_events`` and return its id.

    The id is the value the SSE handler stamps onto its ``id:`` line; the
    client echoes it back as ``Last-Event-ID`` to resume after a disconnect.
    It is PER-TARGET (contract 1 in the module docstring), so two agents both
    have an event 1 and ``mark_delivered`` needs the target to tell them
    apart.

    ``event`` is the minted envelope produced by
    :func:`a2a._inbox_bus.mint_event` — stored verbatim in ``meta_json`` so
    the replay path yields byte-identical frames.

    Allocation and insert share ONE transaction. That is what makes the
    counter row a serialisation point rather than a hint: the row lock the
    allocation takes is held until the event row is in, so a concurrent
    writer for the same target cannot interleave between them.
    """
    source = event.get("from_agent")
    kind = event.get("kind") or "message"
    content = event.get("content")
    ts = float(event.get("ts") or time.time())
    # NEVER a JSON codec on the way in or out — see the store module. This
    # exact string is what comes back, byte for byte.
    meta_json = json.dumps(event, ensure_ascii=False)

    def _op(conn: "psycopg.Connection") -> int:
        with conn.transaction():
            row = conn.execute(_ALLOCATE_SQL, (target,)).fetchone()
            if not row or not row[0]:
                # Loud failure (handoff §0 Hard rules): if the counter did
                # not mint an id, something is structurally wrong — surface
                # it instead of returning a sentinel that callers might
                # confuse with a real cursor.
                raise RuntimeError(
                    "sac_channel_cursor allocation did not yield an id for "
                    f"target {target!r} — schema mismatch?"
                )
            row_id = int(row[0])
            conn.execute(
                _INSERT_SQL,
                (target, row_id, source, kind, content, meta_json, ts),
            )
            return row_id

    row_id = int(run_with_reconnect(_op))
    if row_id <= 0:
        raise RuntimeError(
            "sac_channel_cursor allocation yielded a non-positive id "
            f"({row_id}) for target {target!r} — schema mismatch?"
        )
    return row_id


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


def list_undelivered(*, target: str) -> list[dict[str, Any]]:
    """Return every undelivered event for ``target`` in id order.

    Each dict carries ``{"id", "event"}``: ``id`` is the SSE cursor,
    ``event`` is the round-tripped envelope.

    Served by ``sac_channel_events_undelivered_idx``, a partial index over
    exactly this predicate, so a fresh subscriber's replay scans only the
    rows still waiting rather than the target's whole history.
    """

    def _op(conn: "psycopg.Connection") -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, meta_json FROM sac_channel_events "
            "WHERE target = %s AND delivered_at IS NULL ORDER BY id ASC",
            (target,),
        ).fetchall()
        return [{"id": int(r[0]), "event": json.loads(r[1])} for r in rows]

    return run_with_reconnect(_op)


def list_since_id(*, target: str, since_id: int) -> list[dict[str, Any]]:
    """Return every event for ``target`` whose id is strictly greater than
    ``since_id``, in id order.

    Used by the ``Last-Event-ID`` SSE reconnect path: the client passes the
    highest id it has *already seen* so it resumes at the next event without
    re-receiving the cursor itself. ``delivered_at`` is deliberately NOT
    consulted — a client that saw a frame and then dropped still wants the
    frames after it, whether or not another subscriber has since marked them.
    """

    def _op(conn: "psycopg.Connection") -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, meta_json FROM sac_channel_events "
            "WHERE target = %s AND id > %s ORDER BY id ASC",
            (target, int(since_id)),
        ).fetchall()
        return [{"id": int(r[0]), "event": json.loads(r[1])} for r in rows]

    return run_with_reconnect(_op)


# --------------------------------------------------------------------------
# deliver
# --------------------------------------------------------------------------


def mark_delivered(row_ids: Iterable[int], *, target: str) -> None:
    """Set ``delivered_at = now`` on ``target``'s rows whose id is in
    ``row_ids`` AND whose ``delivered_at`` is still NULL.

    ``target`` IS REQUIRED, AND THAT IS A CORRECTNESS FIX, NOT ERGONOMICS.
    Ids used to be globally unique (one ``AUTOINCREMENT`` sequence), so
    ``WHERE id IN (...)`` named exactly the rows the caller had just shipped.
    They are now PER-TARGET, so agent ``A`` and agent ``B`` both have an
    event ``1`` — and the old predicate would mark ``B``'s event delivered
    while ``B`` was disconnected, deleting it from ``B``'s
    fresh-subscriber replay with no error anywhere. Every call site has the
    target name in scope; requiring it makes the mistake unrepresentable.

    Idempotent — calling twice does not move the timestamp. The
    ``delivered_at IS NULL`` predicate keeps the "first-delivery" semantics
    the handoff calls out ("Mark delivered exactly once (delivered_at)").
    """
    ids = [int(i) for i in row_ids]
    if not ids:
        return
    now = time.time()

    def _op(conn: "psycopg.Connection") -> None:
        with conn.transaction():
            conn.execute(
                "UPDATE sac_channel_events SET delivered_at = %s "
                "WHERE target = %s AND id = ANY(%s) AND delivered_at IS NULL",
                (now, target, ids),
            )

    run_with_reconnect(_op)


# --------------------------------------------------------------------------
# rename
# --------------------------------------------------------------------------


class ChannelRename:
    """The exact inverse of one completed :func:`rename_channel_events`.

    Holds the ids the rename actually touched, so the undo is scoped to
    those rows rather than to a ``WHERE target = new`` predicate that would
    also clobber rows which legitimately held ``new`` before — the same trap
    the original's row-id capture existed to avoid.
    """

    __slots__ = ("old", "new", "offset", "target_ids", "source_ids")

    def __init__(
        self,
        *,
        old: str,
        new: str,
        offset: int,
        target_ids: list[int],
        source_ids: list[tuple[str, int]],
    ) -> None:
        self.old = old
        self.new = new
        #: How far the migrated ids were shifted. ZERO in the ordinary case
        #: (nothing already lived under ``new``), which is what keeps a live
        #: consumer's ``Last-Event-ID`` valid across a rename.
        self.offset = offset
        #: Post-rename ids of the rows whose ``target`` moved.
        self.target_ids = target_ids
        #: ``(target, id)`` of the rows whose ``source`` moved.
        self.source_ids = source_ids

    @property
    def total(self) -> int:
        return len(self.target_ids) + len(self.source_ids)


def rename_channel_events(*, old: str, new: str) -> ChannelRename | None:
    """Point ``old``'s message history at ``new``. Returns the inverse.

    A renamed agent is the SAME agent — its past must still be findable
    under the new name. This is the ``git mv`` position, and it is why
    ``channel_events`` was in ``_rename_db.NAME_COLUMNS`` for as long as
    that module existed.

    IT IS AN EXPLICIT STEP RATHER THAN A ``NAME_COLUMNS`` PAIR, and the
    reason is the same one that moved ``comms_nodes`` and
    ``node_comms_policy`` out of that tuple on 2026-08-28: ``rename_rows``
    SKIPPED any table the schema no longer declared. Leaving the two pairs
    behind
    would have made ``sac agents rename`` report success while the agent's
    entire message history stayed under the old name — a silent no-op, which
    is worse than a crash because nobody looks.

    THE ID SHIFT. ``(target, id)`` is the primary key, so if rows already
    exist under ``new`` (the leftovers of a previously deleted agent by that
    name) the two id spaces collide. Every migrated id is therefore shifted
    above ``new``'s current maximum. In the ordinary case that maximum does
    not exist and the offset is ZERO, so ids are preserved exactly and a
    consumer holding a ``Last-Event-ID`` resumes without a gap.

    Returns ``None`` when there was nothing to move (no rows, no cursor) so
    the caller can skip pushing an inverse, matching ``rename_comms_node``'s
    falsy-means-nothing-happened shape.
    """
    if old == new:
        return None

    def _op(conn: "psycopg.Connection") -> ChannelRename | None:
        with conn.transaction():
            offset = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM sac_channel_events "
                    "WHERE target = %s",
                    (new,),
                ).fetchone()[0]
            )
            target_ids = [
                int(r[0])
                for r in conn.execute(
                    "UPDATE sac_channel_events SET target = %s, id = id + %s "
                    "WHERE target = %s RETURNING id",
                    (new, offset, old),
                ).fetchall()
            ]
            # ``source`` is rewritten AFTER the retarget, so a row that was
            # both sent BY ``old`` and TO ``old`` is caught by both passes.
            source_ids = [
                (str(r[0]), int(r[1]))
                for r in conn.execute(
                    "UPDATE sac_channel_events SET source = %s "
                    "WHERE source = %s RETURNING target, id",
                    (new, old),
                ).fetchall()
            ]
            moved_cursor = _carry_cursor(conn, old=old, new=new, offset=offset)
        if not target_ids and not source_ids and not moved_cursor:
            return None
        return ChannelRename(
            old=old,
            new=new,
            offset=offset,
            target_ids=target_ids,
            source_ids=source_ids,
        )

    return run_with_reconnect(_op)


def _carry_cursor(
    conn: "psycopg.Connection", *, old: str, new: str, offset: int
) -> bool:
    """Raise ``new``'s counter above every id that now exists under it.

    ``old``'s counter row is deliberately LEFT IN PLACE. It costs one row and
    it guarantees that an agent later created with the old name never reuses
    an id the old agent's consumers already saw — monotonicity is per NAME,
    not per incarnation, and gaps are free (contract 1).
    """
    row = conn.execute(
        "SELECT next_id FROM sac_channel_cursor WHERE target = %s", (old,)
    ).fetchone()
    if row is None:
        return False
    carried = int(row[0]) + offset
    conn.execute(
        "INSERT INTO sac_channel_cursor (target, next_id) VALUES (%s, %s) "
        "ON CONFLICT (target) DO UPDATE SET next_id = "
        "GREATEST(sac_channel_cursor.next_id, EXCLUDED.next_id)",
        (new, carried),
    )
    return True


def undo_rename_channel_events(undo: ChannelRename) -> None:
    """Restore every row :func:`rename_channel_events` touched, by id.

    ``new``'s counter is left where the rename raised it to. Winding a
    counter BACK is the one thing this must never do: a later event would
    then reuse an id a consumer has already seen, which is the frame-dropping
    failure the whole per-target cursor exists to prevent. An over-high
    counter costs a gap, and gaps are free.
    """
    if undo.total == 0:
        return

    def _op(conn: "psycopg.Connection") -> None:
        with conn.transaction():
            if undo.source_ids:
                conn.execute(
                    "UPDATE sac_channel_events SET source = %s "
                    "WHERE (target, id) IN ("
                    "  SELECT * FROM UNNEST(%s::text[], %s::bigint[]))",
                    (
                        undo.old,
                        [t for t, _ in undo.source_ids],
                        [i for _, i in undo.source_ids],
                    ),
                )
            if undo.target_ids:
                conn.execute(
                    "UPDATE sac_channel_events SET target = %s, id = id - %s "
                    "WHERE target = %s AND id = ANY(%s)",
                    (undo.old, undo.offset, undo.new, undo.target_ids),
                )

    run_with_reconnect(_op)
