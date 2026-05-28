"""ADR-0014 — ``comms_nodes`` primitives + resolver fallback.

Covers :mod:`scitex_agent_container._state.state_db_comms_nodes` and the
``resolve_node_host`` / ``is_local_node`` extensions in
:mod:`scitex_agent_container._state.state_db_nodes`.

PA-306 conventions: no mocks; real on-disk SQLite under ``tmp_path``;
AAA structure; one logical assertion per test.
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
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='comms_nodes'"
        ).fetchall()
    # Assert
    assert len(rows) == 1


@pytest.mark.parametrize(
    "column",
    [
        "name",
        "host",
        "a2a_port",
        "registered_at",
        "updated_at",
        "source_host",
        "ended_at",
    ],
)
def test_comms_nodes_has_column(db_path: Path, column: str) -> None:
    # Arrange
    conn_ctx = state_db.open_db(db_path)
    # Act
    with conn_ctx as conn:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(comms_nodes)").fetchall()
        }
    # Assert
    assert column in cols


def test_known_tables_includes_comms_nodes() -> None:
    # Arrange + Act
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Assert
    assert "comms_nodes" in KNOWN_TABLES


# ---------------------------------------------------------------------------
# register_comms_node — fresh insert + idempotent re-register
# ---------------------------------------------------------------------------


def test_register_comms_node_inserts_row(db_path: Path) -> None:
    # Arrange + Act
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Assert
    info = lookup_comms_node(name="lead", db_path=db_path)
    assert info is not None and info["host"] == "mba" and info["a2a_port"] == 8642


def test_register_comms_node_source_host_stored(db_path: Path) -> None:
    # Arrange + Act
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host="spartan",
        db_path=db_path,
    )
    # Assert
    info = lookup_comms_node(name="lead", db_path=db_path)
    assert info["source_host"] == "spartan"


def test_register_comms_node_idempotent_same_target(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    first = lookup_comms_node(name="lead", db_path=db_path)
    # Act
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    second = lookup_comms_node(name="lead", db_path=db_path)
    # Assert — same row, updated_at refreshed (>= first)
    assert second["updated_at"] >= first["updated_at"]


def test_register_comms_node_same_source_can_update_target(db_path: Path) -> None:
    # Arrange — operator rebound listen on a different port
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
    )
    # Assert
    info = lookup_comms_node(name="lead", db_path=db_path)
    assert info["a2a_port"] == 9000


def test_register_comms_node_conflict_different_source_raises(
    db_path: Path,
) -> None:
    # Arrange — local registration of lead@mba.
    register_comms_node(
        name="lead",
        host="mba",
        a2a_port=8642,
        source_host=None,
        db_path=db_path,
    )
    # Act + Assert — Spartan trying to register lead with a different host:port.
    with pytest.raises(CommsNodeConflictError) as excinfo:
        register_comms_node(
            name="lead",
            host="spartan",
            a2a_port=8642,
            source_host="spartan",
            db_path=db_path,
        )
    assert "lead" in str(excinfo.value)


def test_register_comms_node_rejects_empty_name(db_path: Path) -> None:
    with pytest.raises(ValueError):
        register_comms_node(name="", host="mba", a2a_port=8642, db_path=db_path)


def test_register_comms_node_rejects_zero_port(db_path: Path) -> None:
    with pytest.raises(ValueError):
        register_comms_node(name="lead", host="mba", a2a_port=0, db_path=db_path)


# ---------------------------------------------------------------------------
# unregister + tombstones + lookup filters
# ---------------------------------------------------------------------------


def test_unregister_comms_node_tombstones_row(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Act
    removed = unregister_comms_node(name="lead", db_path=db_path)
    # Assert
    assert removed is True
    assert lookup_comms_node(name="lead", db_path=db_path) is None


def test_unregister_comms_node_double_call_returns_false(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    second = unregister_comms_node(name="lead", db_path=db_path)
    # Assert
    assert second is False


def test_register_reactivates_tombstoned_row(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    unregister_comms_node(name="lead", db_path=db_path)
    # Act — same (host, port) re-register clears the tombstone.
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Assert
    info = lookup_comms_node(name="lead", db_path=db_path)
    assert info is not None and info["ended_at"] is None


def test_list_comms_nodes_filters_tombstones_by_default(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="alpha", host="h1", a2a_port=7000, db_path=db_path
    )
    register_comms_node(
        name="beta", host="h2", a2a_port=7001, db_path=db_path
    )
    unregister_comms_node(name="beta", db_path=db_path)
    # Act
    rows = list_comms_nodes(db_path=db_path)
    # Assert
    names = sorted(r["name"] for r in rows)
    assert names == ["alpha"]


def test_list_comms_nodes_include_ended_returns_all(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="alpha", host="h1", a2a_port=7000, db_path=db_path
    )
    register_comms_node(
        name="beta", host="h2", a2a_port=7001, db_path=db_path
    )
    unregister_comms_node(name="beta", db_path=db_path)
    # Act
    rows = list_comms_nodes(db_path=db_path, include_ended=True)
    # Assert
    names = sorted(r["name"] for r in rows)
    assert names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# resolve_node_host — fallback to comms_nodes after instances misses
# ---------------------------------------------------------------------------


def test_resolve_node_host_returns_none_for_unknown_name(db_path: Path) -> None:
    # Arrange + Act + Assert
    assert resolve_node_host(name="ghost", db_path=db_path) is None


def test_resolve_node_host_finds_comms_nodes_when_no_instance(
    db_path: Path,
) -> None:
    # Arrange — no instances row; only a comms_nodes row.
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info == {"host": "mba", "a2a_port": 8642}


def test_resolve_node_host_instances_wins_when_both_present(
    db_path: Path,
) -> None:
    # Arrange — an active instances row should take precedence over
    # any comms_nodes entry (live instance is more authoritative than
    # a sync'd federated row).
    from scitex_agent_container._state.state_db import record_instance_start

    state_db.DEFAULT_DB_PATH  # touch attribute to ensure import path stable
    # Manually point record_instance_start at our temp DB by env, which the
    # state_db module respects via DEFAULT_DB_PATH at import time. The
    # fixture has already run init_schema(p), but the writer module reads
    # DEFAULT_DB_PATH lazily — so we must INSERT directly to control where.
    import time as _time

    with state_db.open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (id, name, host, scope, a2a_port, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("inst-1", "lead", "instances-host", "global", 9000, _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
            )),
        )
    register_comms_node(
        name="lead", host="comms-host", a2a_port=8642, db_path=db_path
    )
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info["host"] == "instances-host"
    del record_instance_start  # silence unused import warning


def test_resolve_node_host_skips_tombstoned_comms_node(db_path: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    unregister_comms_node(name="lead", db_path=db_path)
    # Act
    info = resolve_node_host(name="lead", db_path=db_path)
    # Assert
    assert info is None


# ---------------------------------------------------------------------------
# is_local_node — federated graph correctly identifies cross-host targets
# ---------------------------------------------------------------------------


def test_is_local_node_true_when_comms_node_matches_local_host(
    db_path: Path,
) -> None:
    # Arrange
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Act + Assert
    assert is_local_node(name="lead", local_host="mba", db_path=db_path)


def test_is_local_node_false_when_comms_node_lives_elsewhere(
    db_path: Path,
) -> None:
    # Arrange — Spartan's view: lead lives on mba, but we are spartan.
    register_comms_node(
        name="lead", host="mba", a2a_port=8642, db_path=db_path
    )
    # Act + Assert — this is the cross-host bug-fix assertion.
    assert not is_local_node(name="lead", local_host="spartan", db_path=db_path)


def test_is_local_node_true_for_unknown_name(db_path: Path) -> None:
    # Arrange + Act + Assert — unknown names fall back to local
    # (NodeRegistry implicit-register path).
    assert is_local_node(name="ghost", local_host="mba", db_path=db_path)
