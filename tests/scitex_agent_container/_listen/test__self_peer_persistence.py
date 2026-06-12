"""Tests for ``_listen._self_peer_persistence`` (Q4: persist discovered
self-peers into ``comms_nodes`` so the federated graph survives a
listen restart).

Lead dispatch a2a c8b64f298b8a...: the channel-side
``_channel_self_register`` already UPSERTs a row for the running
``sac mcp channel`` session. Q4 mirrors that on the listen side —
every self-peer the cwd-walk discovers (``agents/self/spec.yaml``
under any search dir) lands in ``comms_nodes`` at listen startup so
``sac a2a peers`` keeps reporting them across reboots.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — real state.db under
``tmp_path``, real ``discover_self_peers`` walk where it matters, and
no ``monkeypatch`` fixture (a yield-fixture pair saves and restores
the state-db env var + module attribute exactly like the
``cross_host_env`` pattern in ``test_server.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._listen._self_peer_persistence import (
    persist_discovered_self_peers,
)
from scitex_agent_container._state import state_db as _state_db
from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    register_comms_node,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_STATE_DB_ENV = "SCITEX_AGENT_CONTAINER_STATE_DB"


def _swap_env(name: str, value: str | None) -> str | None:
    """Set or unset env var ``name``; return prior value for restore."""
    prev = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return prev


@pytest.fixture
def isolated_state_db(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``state.db`` writes to a per-test tmp file (no ``monkeypatch``)."""
    db = tmp_path / "state.db"
    prev_env = _swap_env(_STATE_DB_ENV, str(db))
    prev_attr = _state_db.DEFAULT_DB_PATH
    _state_db.DEFAULT_DB_PATH = db
    _state_db.init_schema(db)
    try:
        yield db
    finally:
        _state_db.DEFAULT_DB_PATH = prev_attr
        _swap_env(_STATE_DB_ENV, prev_env)


def _count_comms_nodes(db_path: Path, name: str | None = None) -> int:
    """Helper — count rows in ``comms_nodes``, optionally filtered by name."""
    from scitex_agent_container._state.state_db import open_db

    with open_db(db_path) as conn:
        if name is None:
            return conn.execute("SELECT COUNT(*) FROM comms_nodes").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM comms_nodes WHERE name = ?", (name,)
        ).fetchone()[0]


def _fetch_comms_node_port(db_path: Path, name: str) -> int | None:
    """Helper — return ``a2a_port`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT a2a_port FROM comms_nodes WHERE name = ?", (name,)
        ).fetchone()
    return int(row["a2a_port"]) if row is not None else None


def _fetch_comms_node_host(db_path: Path, name: str) -> str | None:
    """Helper — return ``host`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT host FROM comms_nodes WHERE name = ?", (name,)
        ).fetchone()
    return str(row["host"]) if row is not None else None


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — happy path
# ---------------------------------------------------------------------------


def test_persist_writes_a_row_for_a_discovered_self_peer(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers = [
        {"name": "lead", "listen_url": "http://127.0.0.1:7878", "kind": "self-peer"}
    ]
    # Act
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert _count_comms_nodes(isolated_state_db, name="lead") == 1


def test_persist_records_port_parsed_from_listen_url(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers = [{"name": "alpha", "listen_url": "http://10.0.0.1:19042"}]
    # Act
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="alpha-host"
    )
    # Assert
    assert _fetch_comms_node_port(isolated_state_db, "alpha") == 19042


def test_persist_records_canonical_host_argument(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers = [{"name": "beta", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="beta-host"
    )
    # Assert
    assert _fetch_comms_node_host(isolated_state_db, "beta") == "beta-host"


def test_persist_returns_count_of_written_rows(isolated_state_db: Path) -> None:
    # Arrange
    peers = [
        {"name": "a", "listen_url": "http://h:7001"},
        {"name": "b", "listen_url": "http://h:7002"},
        {"name": "c", "listen_url": "http://h:7003"},
    ]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="multi-host"
    )
    # Assert
    assert written == 3


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — idempotence (Q4 lead-mandated guard)
# ---------------------------------------------------------------------------


def test_persist_is_idempotent_no_duplicate_rows_on_second_call(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Act
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert _count_comms_nodes(isolated_state_db, name="lead") == 1


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — skip paths
# ---------------------------------------------------------------------------


def test_persist_skips_peer_with_missing_listen_url(
    isolated_state_db: Path,
) -> None:
    # Arrange — peer dict has no listen_url key.
    peers = [{"name": "ghost"}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_empty_listen_url(isolated_state_db: Path) -> None:
    # Arrange
    peers = [{"name": "ghost", "listen_url": ""}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_zero_port(isolated_state_db: Path) -> None:
    # Arrange: port=0 is the EXACT production-bug signature
    # _channel_self_register was created to close.
    peers = [{"name": "ghost", "listen_url": "http://127.0.0.1:0"}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_portless_listen_url(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers = [{"name": "ghost", "listen_url": "http://127.0.0.1"}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_empty_name(isolated_state_db: Path) -> None:
    # Arrange
    peers = [{"name": "", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — host_config unresolved
# ---------------------------------------------------------------------------


def test_persist_skips_batch_when_canonical_host_is_empty(
    isolated_state_db: Path,
) -> None:
    # Arrange — empty host string means "no canonical host known".
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host=""
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — conflict path
# ---------------------------------------------------------------------------


def test_persist_logs_and_continues_on_comms_node_conflict(
    isolated_state_db: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — pre-existing row from a DIFFERENT source_host claims the name.
    register_comms_node(
        name="lead",
        host="other-host",
        a2a_port=7878,
        source_host="other-host",
        db_path=isolated_state_db,
    )
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    import logging

    caplog.set_level(logging.WARNING)
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="local-host"
    )
    # Assert
    assert written == 0


def test_persist_does_not_raise_on_conflict(isolated_state_db: Path) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="other-host",
        a2a_port=7878,
        source_host="other-host",
        db_path=isolated_state_db,
    )
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    raised: BaseException | None = None
    # Act
    try:
        persist_discovered_self_peers(
            peers, db_path=isolated_state_db, canonical_host="local-host"
        )
    except CommsNodeConflictError as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the function is contracted to NEVER raise — this proves it.)
        raised = exc
    # Assert
    assert raised is None


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — empty input
# ---------------------------------------------------------------------------


def test_persist_returns_zero_for_empty_peer_list(
    isolated_state_db: Path,
) -> None:
    # Arrange
    peers: list[dict] = []
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="test-host"
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# End-to-end via discover_self_peers — real cwd-walk
# ---------------------------------------------------------------------------


def _write_self_spec(root: Path, body: str, dirname: str = "self") -> Path:
    """Helper — drop a ``<root>/<dirname>/spec.yaml`` and return the path."""
    target_dir = root / dirname
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "spec.yaml"
    target.write_text(body)
    return target


def test_persist_end_to_end_with_real_discover_self_peers(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    # Arrange — a real spec.yaml under an agents-base dir.
    from scitex_agent_container._listen._self_peers import discover_self_peers

    _write_self_spec(
        tmp_path,
        "listen_url: http://127.0.0.1:8181\ndescription: e2e test\n",
        dirname="capsule-7",
    )
    peers = discover_self_peers([tmp_path], self_identity=None)
    # Act
    written = persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="e2e-host"
    )
    # Assert
    assert written == 1


def test_persist_end_to_end_records_correct_port(
    isolated_state_db: Path, tmp_path: Path
) -> None:
    # Arrange
    from scitex_agent_container._listen._self_peers import discover_self_peers

    _write_self_spec(
        tmp_path, "listen_url: http://127.0.0.1:8181\n", dirname="capsule-9"
    )
    peers = discover_self_peers([tmp_path], self_identity=None)
    # Act
    persist_discovered_self_peers(
        peers, db_path=isolated_state_db, canonical_host="e2e-host"
    )
    # Assert
    assert _fetch_comms_node_port(isolated_state_db, "capsule-9") == 8181
