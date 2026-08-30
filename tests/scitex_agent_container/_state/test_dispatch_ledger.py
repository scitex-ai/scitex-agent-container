"""Tests for the dispatch ledger (2026-05-22), on PostgreSQL since 2026-08-28.

Every outbound dispatch gets a stable ``dispatch_id`` minted at the sender and
persisted to the ``dispatches`` store so dispatches can be filtered and
recalled later.

``db_path`` IS GONE from every call. It named a file and there is no
file; every test that used to thread ``tmp_path / "state.db"`` now takes
``pg_schema`` instead, which is the seam keeping one test's rows out of
another's — and out of the live fleet store.

THE ONE TEST THAT CHANGED SHAPE rather than just its fixture is the schema
one. It used to open ``state.db`` directly and look for a table named
``dispatches`` in the schema catalogue; there is no file to open, so it now reads
the tables PostgreSQL actually holds through a SECOND, INDEPENDENT client —
raw psycopg, plain SQL. Asking the store whether its own write landed cannot
distinguish "wrote to PostgreSQL" from "wrote somewhere else", which is
exactly how a store can look healthy while sharing nothing.

Conventions (mirroring test_state_db_turns_errors_heartbeats.py):

  * One assertion per test (STX-TQ007); related invariants collapse into
    ``pytest.parametrize``.
  * AAA markers (Arrange / Act / Assert).
  * No mocks / monkeypatch (STX-NM); a real PostgreSQL schema per test.
"""

from __future__ import annotations

import psycopg
import pytest

from tests._store_isolation import PG_BASE_DSN as _BASE_DSN
from tests._store_isolation import pg_endpoint_port


def _tables_in(schema: str) -> set[str]:
    """The table names PostgreSQL actually holds in ``schema``."""
    with psycopg.connect(_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Schema — the dispatches store is created on first use.
# ---------------------------------------------------------------------------


def test_init_ledger_schema_creates_the_dispatches_store_in_postgres(pg_schema: str):
    """POSITIVE CONTROL for every test below — read by an independent client."""
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import init_ledger_schema

    expected = "dispatches_rows"
    # Act
    init_ledger_schema()
    # Assert
    assert expected in _tables_in(pg_schema)


def test_init_ledger_schema_reports_where_the_state_went(pg_schema: str):
    """The return value NAMES the target, so a caller can check it.

    The previous implementation returned a Path for the same reason. A store that
    cannot say where it is is a store nobody can verify.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import init_ledger_schema

    expected_fragment = pg_endpoint_port()
    # Act
    locator = init_ledger_schema()
    # Assert
    assert expected_fragment in locator


def test_record_dispatch_creates_the_store_lazily_without_explicit_init(
    pg_schema: str,
):
    # Arrange — never call init_ledger_schema; record_dispatch must ensure the
    # store itself.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    # Act
    record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    rows = list_dispatches()
    # Assert
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Mint — new_dispatch_id is a 32-char uuid4 hex.
# ---------------------------------------------------------------------------


def test_new_dispatch_id_is_32_char_hex():
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import new_dispatch_id

    # Act
    did = new_dispatch_id()
    # Assert
    assert len(did) == 32 and all(c in "0123456789abcdef" for c in did)


def test_new_dispatch_id_is_unique_across_calls():
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import new_dispatch_id

    # Act
    ids = {new_dispatch_id() for _ in range(1000)}
    # Assert
    assert len(ids) == 1000


# ---------------------------------------------------------------------------
# Round-trip — record_dispatch writes a row a read can recover.
# ---------------------------------------------------------------------------


def test_record_dispatch_returns_the_minted_id(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    did = record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    # Assert
    assert len(did) == 32


def test_record_dispatch_honours_supplied_id(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    did = record_dispatch(
        from_agent="alice", to_agent="bob", text="hi", dispatch_id="deadbeef"
    )
    # Assert
    assert did == "deadbeef"


def test_record_dispatch_round_trips_to_agent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="bob", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["to_agent"] == "bob"


def test_record_dispatch_round_trips_conversation_id(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="hi", conversation_id="conv-7")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["conversation_id"] == "conv-7"


def test_record_dispatch_round_trips_the_owning_agent(pg_schema: str):
    # Arrange — the field the fleet-wide store needed; see the module
    # docstring of _state/dispatch_ledger_store.py.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(agent="owner-1", from_agent="a", to_agent="b", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["agent"] == "owner-1"


def test_record_dispatch_without_an_owner_records_it_unowned(pg_schema: str):
    # Arrange — an ops script outside a container has no owner to name. Store
    # IDENTITY fields must be present, so "" is what "nobody's" looks like.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["agent"] == ""


def test_record_dispatch_defaults_status_to_sent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        STATUS_SENT,
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == STATUS_SENT


def test_record_dispatch_allows_null_agents(pg_schema: str):
    # Arrange — a script driving post_turn outside an agent has no
    # SAC_NAME; the ledger row must still record (from_agent=None).
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent=None, to_agent=None, text="hi")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["from_agent"] is None


# ---------------------------------------------------------------------------
# text_summary truncation — long bodies are clipped to the first 500 chars.
# ---------------------------------------------------------------------------


def test_record_dispatch_truncates_long_text_summary(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="x" * 5000)
    # Act
    rows = list_dispatches()
    # Assert
    assert len(rows[0]["text_summary"]) == 500


def test_record_dispatch_keeps_short_text_intact(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="short")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["text_summary"] == "short"


# ---------------------------------------------------------------------------
# Status validation — unknown statuses fail loudly (no silent write).
# ---------------------------------------------------------------------------


def test_record_dispatch_rejects_unknown_status(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import record_dispatch

    # Act
    ctx = pytest.raises(ValueError, match="unknown dispatch status")
    # Assert
    with ctx:
        record_dispatch(from_agent="a", to_agent="b", text="hi", status="bogus")


def test_update_dispatch_status_rejects_unknown_status(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    ctx = pytest.raises(ValueError, match="unknown dispatch status")
    # Assert
    with ctx:
        update_dispatch_status(did, "bogus")


# ---------------------------------------------------------------------------
# Status transition — sent -> delivered/timeout/failed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal",
    ["delivered", "timeout", "failed"],
)
def test_update_dispatch_status_transitions_to_terminal(pg_schema: str, terminal: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, terminal)
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == terminal


def test_update_dispatch_status_keyed_by_owner_transitions_the_row(pg_schema: str):
    # Arrange — the O(1) path: the caller names the owner, so the update is a
    # keyed write rather than a scan.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(agent="owner-1", from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, "delivered", agent="owner-1")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == "delivered"


def test_update_dispatch_status_finds_an_owned_row_without_being_told_the_owner(
    pg_schema: str,
):
    # Arrange — the O(n) path. It must NOT guess the unowned key: a row owned
    # by "owner-1" would then silently match nothing and the status would
    # never move, with no error anywhere.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(agent="owner-1", from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, "delivered")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == "delivered"


def test_update_dispatch_status_returns_true_on_match(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(from_agent="a", to_agent="b", text="hi")
    # Act
    matched = update_dispatch_status(did, "delivered")
    # Assert
    assert matched is True


def test_update_dispatch_status_returns_false_on_missing_row(pg_schema: str):
    # Arrange — no row with this id exists.
    from scitex_agent_container._state.dispatch_ledger import update_dispatch_status

    # Act
    matched = update_dispatch_status("does-not-exist", "delivered")
    # Assert
    assert matched is False


def test_update_dispatch_status_falls_back_when_the_named_owner_is_wrong(
    pg_schema: str,
):
    """A disagreement about "who am I" must not silently lose the update.

    ``agent=`` is a FAST PATH, not a filter. Two production resolvers answer
    this question — ``SAC_NAME`` on the peer client, ``--name`` or the
    discovered self spec in the MCP channel — so a keyed miss is a reachable
    state, and the pre-migration behaviour (find the row by its unique uuid4)
    is what must survive. Written as its own test because the strict version
    passed every unit test here and only failed end-to-end, in
    ``test_inbound_reaction_updates_dispatch_ledger``.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(agent="owner-1", from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, "delivered", agent="owner-2")
    # Act
    rows = list_dispatches()
    # Assert
    assert rows[0]["status"] == "delivered"


def test_update_dispatch_status_does_not_invent_an_owner_for_the_row(pg_schema: str):
    """The fallback finds the row; it must not RE-KEY it under the caller.

    ``agent`` is IMMUTABLE and half the identity, so a fallback that wrote the
    caller's name instead of the row's would silently move somebody's dispatch
    into somebody else's recall — the exact leak this port closed, arriving
    through the write path instead.
    """
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
        update_dispatch_status,
    )

    did = record_dispatch(agent="owner-1", from_agent="a", to_agent="b", text="hi")
    update_dispatch_status(did, "delivered", agent="owner-2")
    # Act
    rows = list_dispatches(agent="owner-1")
    # Assert
    assert [r["dispatch_id"] for r in rows] == [did]


# ---------------------------------------------------------------------------
# Query filters — from / to / status / conversation / since / limit.
# ---------------------------------------------------------------------------


def test_list_dispatches_filter_by_from_agent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="x", text="1")
    record_dispatch(from_agent="bob", to_agent="x", text="2")
    record_dispatch(from_agent="alice", to_agent="y", text="3")
    # Act
    rows = list_dispatches(from_agent="alice")
    # Assert
    assert {r["text_summary"] for r in rows} == {"1", "3"}


def test_list_dispatches_filter_by_to_agent(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="bob", text="1")
    record_dispatch(from_agent="a", to_agent="carol", text="2")
    # Act
    rows = list_dispatches(to_agent="bob")
    # Assert
    assert [r["text_summary"] for r in rows] == ["1"]


def test_list_dispatches_filter_by_status(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="1", status="sent")
    record_dispatch(from_agent="a", to_agent="b", text="2", status="failed")
    # Act
    rows = list_dispatches(status="failed")
    # Assert
    assert [r["text_summary"] for r in rows] == ["2"]


def test_list_dispatches_filter_by_conversation_id(pg_schema: str):
    # Arrange — "which dispatches belonged to this conversation?"
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="1", conversation_id="c1")
    record_dispatch(from_agent="a", to_agent="b", text="2", conversation_id="c2")
    record_dispatch(from_agent="a", to_agent="b", text="3", conversation_id="c1")
    # Act
    rows = list_dispatches(conversation_id="c1")
    # Assert
    assert {r["text_summary"] for r in rows} == {"1", "3"}


def test_list_dispatches_filter_by_since(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="old", ts=100.0)
    record_dispatch(from_agent="a", to_agent="b", text="new", ts=200.0)
    # Act
    rows = list_dispatches(since=150.0)
    # Assert
    assert [r["text_summary"] for r in rows] == ["new"]


def test_list_dispatches_combined_filters_are_anded(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="alice", to_agent="bob", text="hit", status="failed")
    record_dispatch(from_agent="alice", to_agent="bob", text="miss-status")
    record_dispatch(from_agent="zed", to_agent="bob", text="miss-from", status="failed")
    # Act
    rows = list_dispatches(from_agent="alice", to_agent="bob", status="failed")
    # Assert
    assert [r["text_summary"] for r in rows] == ["hit"]


def test_list_dispatches_orders_newest_first(pg_schema: str):
    # Arrange — `Store.rows()` is NOT ordered, so this pins the sort rather
    # than whatever order PostgreSQL happened to return.
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="first", ts=10.0)
    record_dispatch(from_agent="a", to_agent="b", text="second", ts=20.0)
    # Act
    rows = list_dispatches()
    # Assert
    assert [r["text_summary"] for r in rows] == ["second", "first"]


def test_list_dispatches_respects_limit(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    for i in range(5):
        record_dispatch(from_agent="a", to_agent="b", text=str(i), ts=float(i))
    # Act
    rows = list_dispatches(limit=2)
    # Assert
    assert len(rows) == 2


def test_list_dispatches_empty_when_no_match(pg_schema: str):
    # Arrange
    from scitex_agent_container._state.dispatch_ledger import (
        list_dispatches,
        record_dispatch,
    )

    record_dispatch(from_agent="a", to_agent="b", text="x")
    # Act
    rows = list_dispatches(from_agent="nobody")
    # Assert
    assert rows == []
