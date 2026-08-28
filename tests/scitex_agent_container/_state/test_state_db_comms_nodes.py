"""ADR-0014 — ``comms_nodes`` primitives + resolver fallback.

Covers :mod:`scitex_agent_container._state.state_db_comms_nodes` and the
``resolve_node_host`` / ``is_local_node`` extensions in
:mod:`scitex_agent_container._state.state_db_nodes`.

PA-306 conventions: no mocks; real on-disk SQLite under ``tmp_path``;
AAA structure; one assertion per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    is_local_node,
    list_comms_nodes,
    lookup_comms_node,
    register_comms_node,
    resolve_node_host,
    unregister_comms_node,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Arrange
    p = tmp_path / "state.db"
    state_db.init_schema(p)
    return p


# ---------------------------------------------------------------------------
# Schema — comms_nodes table exists with expected columns
# ---------------------------------------------------------------------------


def test_comms_nodes_table_exists(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comms_nodes'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


def test_comms_nodes_has_name_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "name" in cols


def test_comms_nodes_has_host_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "host" in cols


def test_comms_nodes_has_a2a_port_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "a2a_port" in cols


def test_comms_nodes_has_source_host_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "source_host" in cols


def test_comms_nodes_has_ended_at_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "ended_at" in cols


def test_comms_nodes_has_registered_at_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "registered_at" in cols


def test_comms_nodes_has_updated_at_column(db_path: Path) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()}
    # Assert
    assert "updated_at" in cols


def test_known_tables_includes_comms_nodes() -> None:
    # Arrange
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Act
    contains = "comms_nodes" in KNOWN_TABLES
    # Assert
    assert contains


# ---------------------------------------------------------------------------
# register_comms_node — fresh insert + idempotent re-register
# ---------------------------------------------------------------------------


def test_register_comms_node_inserts_row(db_path: Path) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info is not None


def test_register_comms_node_stores_host(db_path: Path) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info["host"] == "mba"


def test_register_comms_node_stores_port(db_path: Path) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info["a2a_port"] == 8642


def test_register_comms_node_source_host_stored(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host="spartan",
        db_path=db_path,
    )
    # Act
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info["source_host"] == "spartan"


def test_register_comms_node_idempotent_same_target(db_path: Path) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    first = lookup_comms_node(name="lead", db_path=db_path)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    second = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert second["updated_at"] >= first["updated_at"]


def test_register_comms_node_same_source_different_target_raises_without_replace(
    db_path: Path,
) -> None:
    # Arrange — PR L1 (operator directive 12847): same-source same-name
    # with a DIFFERENT (host, a2a_port) must NOT silently overwrite.
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
        )
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the function is contracted to raise on same-source different-target without replace=True.)
        raised = exc
    # Assert
    assert isinstance(raised, CommsNodeConflictError)


def test_register_comms_node_same_source_different_target_overwrites_with_replace(
    db_path: Path,
) -> None:
    # Arrange — opt-in via replace=True (wired by upcoming --prefer flag).
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    # Act
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=9000,
        source_host=None,
        db_path=db_path,
        replace=True,
    )
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info["a2a_port"] == 9000


def test_register_comms_node_error_message_names_incoming_kind(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
            kind="self-peer",
        )
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the only thing the assert checks is that the message contains the kind.)
        raised = exc
    # Assert
    assert raised is not None and "self-peer" in str(raised)


def test_register_comms_node_error_message_names_incoming_source_path(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    incoming_path = "/home/agent/proj/foo/agents/lead/spec.yaml"
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
            kind="self-peer",
            source_path=incoming_path,
        )
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and incoming_path in str(raised)


def test_register_comms_node_error_message_names_existing_target(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
        )
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and "8642" in str(raised)


def test_register_comms_node_error_message_mentions_prefer_flag_hint(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
            kind="self-peer",
        )
    except (
        CommsNodeConflictError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert.)
        raised = exc
    # Assert
    assert raised is not None and "--prefer" in str(raised)


def test_register_comms_node_replace_false_does_not_change_row(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    raised: BaseException | None = None
    # Act
    try:
        register_comms_node(
            name="lead",
            host="mba",
            a2a_port=9000,
            source_host=None,
            db_path=db_path,
        )
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; assert is the row stayed unchanged.)
        raised = exc
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert raised is not None and info["a2a_port"] == 8642


def test_register_comms_node_default_kind_keeps_backwards_compat(
    db_path: Path,
) -> None:
    # Arrange — no kwargs beyond the pre-L1 surface; an INSERT must succeed.
    # Act
    register_comms_node(
        name="alpha",
        host="mba",
        a2a_port=12345,
        source_host=None,
        db_path=db_path,
    )
    info = lookup_comms_node(name="alpha", db_path=db_path)
    # Assert
    assert info is not None


def test_register_comms_node_conflict_different_source_raises(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    # Act
    # Assert
    with pytest.raises(CommsNodeConflictError):
        register_comms_node(
            name="lead",
            host="spartan",
            a2a_port=8642,
            source_host="spartan",
            db_path=db_path,
        )


def test_register_comms_node_rejects_empty_name(db_path: Path) -> None:
    # Arrange
    target_db = db_path
    # Act
    # Assert
    with pytest.raises(ValueError):
        register_comms_node(name="", host="mba", a2a_port=8642, db_path=target_db)


def test_register_comms_node_rejects_zero_port(db_path: Path) -> None:
    # Arrange
    target_db = db_path
    # Act
    # Assert
    with pytest.raises(ValueError):
        register_comms_node(name="lead", host="mba", a2a_port=0, db_path=target_db)


# ---------------------------------------------------------------------------
# unregister + tombstones + lookup filters
# ---------------------------------------------------------------------------


def test_unregister_comms_node_returns_true_on_first_call(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    removed = unregister_comms_node(name="lead", db_path=db_path)
    # Assert
    assert removed is True


def test_unregister_comms_node_hides_tombstoned_row_from_lookup(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    after = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert after is None


def test_unregister_comms_node_double_call_returns_false(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    second = unregister_comms_node(name="lead", db_path=db_path)
    # Assert
    assert second is False


def test_register_reactivates_tombstoned_row(db_path: Path) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    info = lookup_comms_node(name="lead", db_path=db_path)
    # Assert
    assert info["ended_at"] is None


def test_list_comms_nodes_filters_tombstones_by_default(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    register_comms_node(name="beta", host="h2", a2a_port=7001, db_path=db_path)
    unregister_comms_node(name="beta", db_path=db_path)
    # Act
    rows = list_comms_nodes(db_path=db_path)
    # Assert
    assert sorted(r["name"] for r in rows) == ["alpha"]


def test_list_comms_nodes_include_ended_returns_all(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="alpha", host="h1", a2a_port=7000, db_path=db_path)
    register_comms_node(name="beta", host="h2", a2a_port=7001, db_path=db_path)
    unregister_comms_node(name="beta", db_path=db_path)
    # Act
    rows = list_comms_nodes(db_path=db_path, include_ended=True)
    # Assert
    assert sorted(r["name"] for r in rows) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# resolve_node_host — fallback to comms_nodes after instances misses
# ---------------------------------------------------------------------------


# These seven take ``pg_schema`` as well as ``db_path``, and the pairing is
# the point rather than boilerplate: since 2026-08-28 ``resolve_node_host``
# consults the PostgreSQL ``instances`` store FIRST and the SQLite
# ``comms_nodes`` table second, so a test of the fall-through has to have
# both engines actually present. With only ``db_path`` the store raises
# StoreTargetError before the fall-through is ever reached.


def test_resolve_node_host_returns_none_for_unknown_name(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    target_db = db_path
    # Act
    info = resolve_node_host(name="ghost", db_path=target_db)
    # Assert
    assert info is None


def test_resolve_node_host_finds_comms_nodes_when_no_instance(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info == {"host": "mba", "a2a_port": 8642}


def test_resolve_node_host_instances_wins_when_both_present(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    import time as _time

    from scitex_agent_container._state.state_db_instances import (
        put_instance_record,
    )

    put_instance_record(
        {
            "id": "inst-1",
            "name": "lead",
            "host": "instances-host",
            "scope": "global",
            "a2a_port": 9000,
            "started_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
    )
    register_comms_node(name="lead", host="comms-host", a2a_port=8642, db_path=db_path)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info["host"] == "instances-host"


def test_resolve_node_host_skips_tombstoned_comms_node(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info is None


# ---------------------------------------------------------------------------
# is_local_node — federated graph correctly identifies cross-host targets
# ---------------------------------------------------------------------------


def test_is_local_node_true_when_comms_node_matches_local_host(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    local = is_local_node(name="lead", local_host="mba", db_path=db_path)
    # Assert
    assert local


def test_is_local_node_false_when_comms_node_lives_elsewhere(
    pg_schema: str,
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(name="lead", host="mba", a2a_port=8642, db_path=db_path)
    # Act
    local = is_local_node(name="lead", local_host="spartan", db_path=db_path)
    # Assert
    assert not local


def test_is_local_node_true_for_unknown_name(
    pg_schema: str, db_path: Path
) -> None:
    # Arrange
    target_db = db_path
    # Act
    local = is_local_node(name="ghost", local_host="mba", db_path=target_db)
    # Assert
    assert local
