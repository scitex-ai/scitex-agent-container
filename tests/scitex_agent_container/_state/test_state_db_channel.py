"""WI-1 — channel-event durability + replay, on PostgreSQL (ADR-0023).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-1 "Durability /
replay-on-reconnect"): every channel event must be persisted so an event
POSTed while no SSE subscriber is connected is delivered on reconnect, and a
kill+reconnect replays exactly the missed events.

These tests drive the persistence primitives against a REAL PostgreSQL
schema — the shared ``pg_schema`` fixture, which points ``SCITEX_STORE_DSN``
at a throwaway schema and drops it afterwards. No mocks, no monkeypatch, no
``db_path``: that argument named a SQLite file and there is no file.

FOUR CONTRACTS, ONE TEST EACH AT MINIMUM
========================================
1. per-target monotonic strictly-increasing ids (gaps allowed);
2. the id is minted server-side and returned synchronously — an allocation
   that yields nothing RAISES rather than returning a sentinel;
3. ``delivered_at`` is first-write-only;
4. ``meta_json`` round-trips byte-identically.

Plus the two hazards the move CREATED, which nothing before it could have:
a composite key (so ``mark_delivered`` must be told the target) and a
connection that can die under a cached handle.

``format_ts_iso`` is a pure display helper and needs no database; its tests
are unchanged and take no fixture.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from scitex_agent_container._state.state_db_channel import (
    format_ts_iso,
    list_since_id,
    list_undelivered,
    mark_delivered,
    persist_event,
)
from scitex_agent_container._state.state_db_channel_store import (
    new_channel_connection,
    reset_channel_connection,
)


@pytest.fixture(autouse=True)
def _drop_cached_connection():
    """Close the process-wide handle around every test in this module.

    The cache is keyed on the resolved target, so a new ``pg_schema`` DSN
    already swaps it — but a test that terminates its own backend, or one
    that leaves a connection open against a schema the fixture is about to
    DROP, needs the handle gone rather than merely superseded.
    """
    reset_channel_connection()
    yield
    reset_channel_connection()


def _event(content: str = "hello", **extra: Any) -> dict[str, Any]:
    """A minted-envelope-shaped event, the shape ``mint_event`` produces."""
    payload: dict[str, Any] = {
        "msg_id": "m-" + content,
        "from_agent": "alice",
        "content": content,
        "kind": "message",
        "ts": 1_700_000_000.5,
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Contract 1 — per-target monotonic ids
# ---------------------------------------------------------------------------


def test_ids_are_per_target_not_global(pg_schema: str) -> None:
    """Interleaved targets each get their OWN 1, 2, 3.

    Under SQLite the id was a global AUTOINCREMENT rowid, so interleaving
    produced 1,3,5 / 2,4,6. The SSE cursor is per-target, so that numbering
    made "resume from 3" mean different things to different agents.
    """
    # Arrange
    order = ["a", "b", "a", "b", "a", "b"]
    # Act
    minted = {"a": [], "b": []}
    for target in order:
        minted[target].append(persist_event(target=target, event=_event(target)))
    # Assert
    assert minted == {"a": [1, 2, 3], "b": [1, 2, 3]}


def test_ids_strictly_increase_for_one_target(pg_schema: str) -> None:
    """Every allocation is strictly greater than the one before it."""
    # Arrange
    target = "solo"
    # Act
    ids = [persist_event(target=target, event=_event(f"e{n}")) for n in range(5)]
    # Assert
    assert ids == sorted(set(ids)) and ids[0] > 0


# ---------------------------------------------------------------------------
# Contract 2 — allocation failure is LOUD
# ---------------------------------------------------------------------------


def test_allocation_that_yields_no_id_raises(pg_schema: str) -> None:
    """An allocation returning no row RAISES; it never returns 0.

    Forced with a real ``BEFORE INSERT``/``BEFORE UPDATE`` trigger that
    returns NULL — PostgreSQL then skips the write and ``RETURNING`` yields
    nothing. Real SQL, no mock: this is the only way to reach the branch
    without patching the module.

    The id is what the SSE handler stamps as the ``id:`` line, so a sentinel
    0 would become a cursor every client resumes from — silently replaying
    the target's entire history on every reconnect.
    """
    # Arrange
    conn = new_channel_connection()
    try:
        conn.execute(
            "CREATE FUNCTION sac_test_suppress() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
        )
        conn.execute(
            "CREATE TRIGGER sac_test_no_alloc BEFORE INSERT OR UPDATE "
            "ON sac_channel_cursor FOR EACH ROW "
            "EXECUTE FUNCTION sac_test_suppress()"
        )
        # Act
        raised: BaseException | None = None
        try:
            persist_event(target="bob", event=_event())
        except RuntimeError as exc:
            raised = exc
    finally:
        conn.close()
    # Assert — ONE assertion (STX-TQ007), and it pins the message as well as
    # the type: "it raised something" would also pass if the allocation blew
    # up for an unrelated reason.
    assert raised is not None and "did not yield an id" in str(raised)


# ---------------------------------------------------------------------------
# Contract 3 — delivered_at is first-write-only
# ---------------------------------------------------------------------------


def test_mark_delivered_does_not_move_an_existing_stamp(pg_schema: str) -> None:
    """A second delivery leaves the first timestamp untouched."""
    # Arrange
    row_id = persist_event(target="bob", event=_event())
    mark_delivered([row_id], target="bob")
    conn = new_channel_connection()
    try:
        first = conn.execute(
            "SELECT delivered_at FROM sac_channel_events "
            "WHERE target = %s AND id = %s",
            ("bob", row_id),
        ).fetchone()[0]
        # Act
        mark_delivered([row_id], target="bob")
        second = conn.execute(
            "SELECT delivered_at FROM sac_channel_events "
            "WHERE target = %s AND id = %s",
            ("bob", row_id),
        ).fetchone()[0]
    finally:
        conn.close()
    # Assert
    assert second == first


def test_marked_row_leaves_the_undelivered_replay(pg_schema: str) -> None:
    """A delivered row is no longer offered to a fresh subscriber."""
    # Arrange
    row_id = persist_event(target="bob", event=_event())
    # Act
    mark_delivered([row_id], target="bob")
    # Assert
    assert list_undelivered(target="bob") == []


def test_delivered_row_is_still_replayed_by_last_event_id(pg_schema: str) -> None:
    """``list_since_id`` ignores ``delivered_at`` — a reconnecting client
    that saw frame N still wants N+1 even if another subscriber marked it."""
    # Arrange
    first = persist_event(target="bob", event=_event("one"))
    second = persist_event(target="bob", event=_event("two"))
    mark_delivered([first, second], target="bob")
    # Act
    resumed = list_since_id(target="bob", since_id=first)
    # Assert
    assert [r["id"] for r in resumed] == [second]


# ---------------------------------------------------------------------------
# The composite-key regression — the hazard the move CREATED
# ---------------------------------------------------------------------------


def test_mark_delivered_does_not_touch_another_target(pg_schema: str) -> None:
    """``mark_delivered([1], target="A")`` leaves ``(B, 1)`` undelivered.

    THE regression test for this PR. Ids used to be globally unique, so
    ``WHERE id IN (...)`` named exactly the rows a stream had shipped. They
    are per-target now, and the old predicate would have marked B's event 1
    delivered while B was disconnected — deleting it from B's
    fresh-subscriber replay with no error anywhere.
    """
    # Arrange
    persist_event(target="A", event=_event("for-a"))
    persist_event(target="B", event=_event("for-b"))
    # Act
    mark_delivered([1], target="A")
    # Assert
    assert [r["id"] for r in list_undelivered(target="B")] == [1]


# ---------------------------------------------------------------------------
# Contract 4 — byte-identical meta_json round-trip
# ---------------------------------------------------------------------------


def test_envelope_round_trips_byte_identically(pg_schema: str) -> None:
    """Japanese content, a nested ``extra``, and a float ts survive exactly.

    ``meta_json`` is TEXT and never routes through a JSON codec: ``sort_keys``
    would reorder keys, ``ensure_ascii`` would escape the Japanese, and a
    ``jsonb`` column would normalise whitespace and re-order the object. A
    replayed frame that is NEARLY the live one is the worst available
    outcome — it looks delivered.
    """
    # Arrange
    event = _event(
        "作業中断はしてほしくない",
        extra={"card_id": "c-1", "nested": {"日本語": ["ア", "イ"], "n": 3}},
        ts=1_777_766_006.95,
    )
    expected = json.dumps(event, ensure_ascii=False)
    # Act
    persist_event(target="bob", event=event)
    conn = new_channel_connection()
    try:
        stored = conn.execute(
            "SELECT meta_json FROM sac_channel_events WHERE target = %s",
            ("bob",),
        ).fetchone()[0]
    finally:
        conn.close()
    # Assert
    assert stored == expected


def test_round_tripped_event_equals_the_original(pg_schema: str) -> None:
    """The decoded replay envelope is equal to what was published."""
    # Arrange
    event = _event("日本語テスト", extra={"deep": {"a": [1, 2.5, None, True]}})
    # Act
    persist_event(target="bob", event=event)
    replayed = list_undelivered(target="bob")
    # Assert
    assert replayed[0]["event"] == event


# ---------------------------------------------------------------------------
# Concurrency — the counter row, not a sequence
# ---------------------------------------------------------------------------


def test_concurrent_writers_get_every_id_exactly_once(pg_schema: str) -> None:
    """N threads persisting to one target mint exactly ``{1..N}``."""
    # Arrange
    n = 12
    minted: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(n)

    def _write(index: int) -> None:
        start.wait()
        row_id = persist_event(target="hot", event=_event(f"e{index}"))
        with guard:
            minted.append(row_id)

    # Act
    threads = [threading.Thread(target=_write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Assert
    assert sorted(minted) == list(range(1, n + 1))


def test_concurrent_writers_leave_readable_id_order(pg_schema: str) -> None:
    """After a concurrent burst, ``list_since_id(0)`` returns 1..N in order.

    THIS IS THE TEST A BARE ``BIGSERIAL`` FAILS. A sequence is
    non-transactional, so id N+1 can become visible before N and a reader
    doing ``id > cursor ORDER BY id`` advances past N and never returns it.
    The counter row's lock makes commit order and id order the same thing.
    """
    # Arrange
    n = 12
    start = threading.Barrier(n)

    def _write(index: int) -> None:
        start.wait()
        persist_event(target="hot", event=_event(f"e{index}"))

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Act
    rows = list_since_id(target="hot", since_id=0)
    # Assert
    assert [r["id"] for r in rows] == list(range(1, n + 1))


def test_separate_connections_never_interleave_ids(pg_schema: str) -> None:
    """Two INDEPENDENT connections allocating for one target serialise.

    The in-process tests above share one handle, so they would pass against a
    sequence too. This one is the cross-connection case a sequence loses:
    each thread holds its OWN connection and runs the module's real
    allocate-then-insert SQL, so nothing but the counter row's lock orders
    them. Reading back through the production reader must still yield a
    dense, ascending run.
    """
    # Arrange
    from scitex_agent_container._state.state_db_channel import (
        _ALLOCATE_SQL,
        _INSERT_SQL,
    )

    n = 6
    start = threading.Barrier(n)

    def _write(index: int) -> None:
        own = new_channel_connection()
        try:
            start.wait()
            with own.transaction():
                row_id = int(own.execute(_ALLOCATE_SQL, ("wide",)).fetchone()[0])
                own.execute(
                    _INSERT_SQL,
                    ("wide", row_id, "alice", "message", f"e{index}", "{}", 1.0),
                )
        finally:
            own.close()

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Act
    rows = list_since_id(target="wide", since_id=0)
    # Assert
    assert [r["id"] for r in rows] == list(range(1, n + 1))


# ---------------------------------------------------------------------------
# A connection that dies under the cached handle
# ---------------------------------------------------------------------------


def test_persist_recovers_after_its_backend_is_terminated(pg_schema: str) -> None:
    """``pg_terminate_backend`` on the cached handle's own backend, then a
    successful write.

    Measured behaviour this guards: psycopg3 never reconnects, so without
    ``run_with_reconnect`` one PostgreSQL restart would break every agent's
    channel until the daemon was restarted by hand.
    """
    # Arrange — force the cached handle to exist, then learn its backend pid.
    persist_event(target="bob", event=_event("before"))
    from scitex_agent_container._state.state_db_channel_store import (
        open_channel_connection,
    )

    pid = int(open_channel_connection().execute("SELECT pg_backend_pid()").fetchone()[0])
    killer = new_channel_connection()
    try:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        killer.close()
    # Act
    row_id = persist_event(target="bob", event=_event("after"))
    # Assert
    assert row_id == 2


# ---------------------------------------------------------------------------
# format_ts_iso — display helper for channel-push timestamps
#
# Storage stays unix-seconds (``sac_channel_events.ts DOUBLE PRECISION``);
# only the rendered/emitted form is ISO-8601. The helper is the canonical
# formatter every display caller routes through (see
# scitex_agent_container._mcp.channel._build_notification). It touches no
# database, so these tests take no fixture.
# ---------------------------------------------------------------------------


def test_format_ts_iso_renders_unix_seconds_as_utc_z() -> None:
    """Float ts (the bus envelope shape) renders as a trailing-Z ISO."""
    # Arrange — 1_700_000_000 is 2023-11-14T22:13:20 UTC.
    # Act
    rendered = format_ts_iso(1_700_000_000.0)
    # Assert — exact-round-trip the canonical formatter emits.
    assert rendered == "2023-11-14T22:13:20Z"


def test_format_ts_iso_renders_int_unix_seconds() -> None:
    """Int ts (e.g. legacy callers) renders the same as float."""
    # Arrange — same unix-seconds value as the float case, as int.
    ts = 1_700_000_000
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert rendered == "2023-11-14T22:13:20Z"


def test_format_ts_iso_matches_iso8601_shape() -> None:
    """Basic ISO-8601 shape regex (date 'T' time, optional fractional
    seconds, optional ``Z`` / ``+HH:MM`` offset)."""
    import re

    # Arrange — a fractional-seconds float value.
    ts = 1_777_766_006.95
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$",
        rendered,
    ), rendered


def test_format_ts_iso_empty_string_stays_empty() -> None:
    """A missing-ts default (the receive-side passes ``event.get('ts', '')``)
    must NOT render as the 1970 epoch."""
    # Arrange — the sentinel empty-string used by missing-ts callers.
    ts = ""
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert rendered == ""


def test_format_ts_iso_none_renders_empty() -> None:
    """A ``None`` ts (missing from envelope) renders empty, same as ``""``."""
    # Arrange — None is the other missing-ts shape envelopes carry.
    ts = None
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert rendered == ""


def test_format_ts_iso_already_iso_string_is_passed_through() -> None:
    """An already-ISO string (a sender that pre-formatted ts) round-trips
    verbatim — composition of render helpers must not corrupt tz."""
    # Arrange
    iso = "2026-04-21T09:30:00+00:00"
    # Act
    rendered = format_ts_iso(iso)
    # Assert
    assert rendered == iso


def test_format_ts_iso_numeric_string_is_coerced_and_rendered() -> None:
    """The JSON-round-trip case: a float ts arrives as ``"1700000000.0"``
    after meta_json (de)serialization. Coerce and render."""
    # Arrange — JSON-serialised float ts shape.
    ts = "1700000000.0"
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert rendered == "2023-11-14T22:13:20Z"


def test_format_ts_iso_does_not_render_bool_as_epoch() -> None:
    """``bool`` is an int subclass — guard so a stray ``True`` does not
    silently become the 1970-01-01T00:00:01Z epoch."""
    # Arrange — bool is the isinstance(_, int) footgun we're guarding.
    ts = True
    # Act
    rendered = format_ts_iso(ts)
    # Assert
    assert rendered == "True"
