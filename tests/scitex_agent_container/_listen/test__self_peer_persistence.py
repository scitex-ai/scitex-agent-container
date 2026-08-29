"""Tests for ``_listen._self_peer_persistence`` (Q4: persist discovered
self-peers into ``comms_nodes`` so the federated graph survives a
listen restart).

Lead dispatch a2a c8b64f298b8a...: the channel-side
``_channel_self_register`` already UPSERTs a row for the running
``sac mcp channel`` session. Q4 mirrors that on the listen side —
every self-peer the cwd-walk discovers (``agents/self/spec.yaml``
under any search dir) lands in ``comms_nodes`` at listen startup so
``sac a2a peers`` keeps reporting them across reboots.

ON POSTGRESQL SINCE 2026-08-28. ``comms_nodes`` moved to the shared store,
so ``persist_discovered_self_peers`` lost its ``db_path`` argument and these
tests take ``pg_schema``. The three ``SELECT ... FROM comms_nodes`` helpers
became calls to ``list_comms_nodes`` / ``lookup_comms_node``: reading a
store's physical table by hand would test scitex-dev's dialect rather than
this module, and the public reader is what production uses.

The ``isolated_state_db`` fixture STAYS. This module still resolves the
state-db env var through ``discover_self_peers``' cwd walk, and pinning it
keeps the walk off the operator's real tree.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — a real store via
``pg_schema``, real ``discover_self_peers`` walk where it matters, and no
``monkeypatch`` fixture (a yield-fixture pair saves and restores the
state-db env var + module attribute exactly like the ``cross_host_env``
pattern in ``test_server.py``).
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
    try:
        yield db
    finally:
        _state_db.DEFAULT_DB_PATH = prev_attr
        _swap_env(_STATE_DB_ENV, prev_env)


def _count_comms_nodes(name: str | None = None) -> int:
    """Helper — count directory entries, optionally filtered by name.

    Reads through the public listing rather than SELECTing the store's rows
    table: the physical layout belongs to scitex-dev, and the reader
    production uses is the one worth asserting against.
    """
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    rows = list_comms_nodes()
    if name is None:
        return len(rows)
    return sum(1 for r in rows if r["name"] == name)


def _fetch_comms_node_port(name: str) -> int | None:
    """Helper — return ``a2a_port`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name=name)
    return None if info is None else int(info["a2a_port"])


def _fetch_comms_node_host(name: str) -> str | None:
    """Helper — return ``host`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name=name)
    return None if info is None else str(info["host"])


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — happy path
# ---------------------------------------------------------------------------


def test_persist_writes_a_row_for_a_discovered_self_peer(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [
        {"name": "lead", "listen_url": "http://127.0.0.1:7878", "kind": "self-peer"}
    ]
    # Act
    persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert _count_comms_nodes(name="lead") == 1


def test_persist_records_port_parsed_from_listen_url(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [{"name": "alpha", "listen_url": "http://10.0.0.1:19042"}]
    # Act
    persist_discovered_self_peers(
        peers, canonical_host="alpha-host"
    )
    # Assert
    assert _fetch_comms_node_port("alpha") == 19042


def test_persist_records_canonical_host_argument(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [{"name": "beta", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    persist_discovered_self_peers(
        peers, canonical_host="beta-host"
    )
    # Assert
    assert _fetch_comms_node_host("beta") == "beta-host"


def test_persist_returns_count_of_written_rows(
    isolated_state_db: Path, pg_schema: str
) -> None:
    # Arrange
    peers = [
        {"name": "a", "listen_url": "http://h:7001"},
        {"name": "b", "listen_url": "http://h:7002"},
        {"name": "c", "listen_url": "http://h:7003"},
    ]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="multi-host"
    )
    # Assert
    assert written == 3


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — idempotence (Q4 lead-mandated guard)
# ---------------------------------------------------------------------------


def test_persist_is_idempotent_no_duplicate_rows_on_second_call(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Act
    persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert _count_comms_nodes(name="lead") == 1


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — skip paths
# ---------------------------------------------------------------------------


def test_persist_skips_peer_with_missing_listen_url(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange — peer dict has no listen_url key.
    peers = [{"name": "ghost"}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_empty_listen_url(
    isolated_state_db: Path, pg_schema: str
) -> None:
    # Arrange
    peers = [{"name": "ghost", "listen_url": ""}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_zero_port(
    isolated_state_db: Path, pg_schema: str
) -> None:
    # Arrange: port=0 is the EXACT production-bug signature
    # _channel_self_register was created to close.
    peers = [{"name": "ghost", "listen_url": "http://127.0.0.1:0"}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_portless_listen_url(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [{"name": "ghost", "listen_url": "http://127.0.0.1"}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_empty_name(
    isolated_state_db: Path, pg_schema: str
) -> None:
    # Arrange
    peers = [{"name": "", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — host_config unresolved
# ---------------------------------------------------------------------------


def test_persist_skips_batch_when_canonical_host_is_empty(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange — empty host string means "no canonical host known".
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host=""
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — conflict path
# ---------------------------------------------------------------------------


def test_persist_logs_and_continues_on_comms_node_conflict(
    isolated_state_db: Path,
    caplog: pytest.LogCaptureFixture,
    pg_schema: str,
) -> None:
    # Arrange — an existing entry claims the name at a DIFFERENT target,
    # which the loud-collision guard refuses without replace=True.
    register_comms_node(
        name="lead",
        host="other-host",
        a2a_port=7878,
        source_host="other-host",
    )
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    import logging

    caplog.set_level(logging.WARNING)
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="local-host"
    )
    # Assert
    assert written == 0


def test_persist_does_not_raise_on_conflict(
    isolated_state_db: Path, pg_schema: str
) -> None:
    # Arrange
    register_comms_node(
        name="lead",
        host="other-host",
        a2a_port=7878,
        source_host="other-host",
    )
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    raised: BaseException | None = None
    # Act
    try:
        persist_discovered_self_peers(
            peers, canonical_host="local-host"
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
    pg_schema: str,
) -> None:
    # Arrange
    peers: list[dict] = []
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


# ---------------------------------------------------------------------------
# skip_names — running listen's own identity must not be double-persisted
# ---------------------------------------------------------------------------


def test_persist_skips_peer_whose_name_is_in_skip_names(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange — the literal self/spec.yaml resolves to the listen's own
    # name ("lead" in production); the listen-side
    # _register_self_comms_node path already owns that row.
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    written = persist_discovered_self_peers(
        peers,
        canonical_host="test-host",
        skip_names=frozenset({"lead"}),
    )
    # Assert
    assert written == 0


def test_persist_skips_only_the_named_peer_and_writes_the_rest(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange — `lead` is skipped, the unrelated `capsule-3` is not.
    peers = [
        {"name": "lead", "listen_url": "http://127.0.0.1:7878"},
        {"name": "capsule-3", "listen_url": "http://10.0.0.7:8181"},
    ]
    # Act
    written = persist_discovered_self_peers(
        peers,
        canonical_host="test-host",
        skip_names=frozenset({"lead"}),
    )
    # Assert
    assert written == 1


def test_persist_skip_names_leaves_skipped_peer_absent_from_db(
    isolated_state_db: Path,
    pg_schema: str,
) -> None:
    # Arrange
    peers = [{"name": "lead", "listen_url": "http://127.0.0.1:7878"}]
    # Act
    persist_discovered_self_peers(
        peers,
        canonical_host="test-host",
        skip_names=frozenset({"lead"}),
    )
    # Assert
    assert _count_comms_nodes(name="lead") == 0


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
    isolated_state_db: Path, tmp_path: Path, pg_schema: str
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
        peers, canonical_host="e2e-host"
    )
    # Assert
    assert written == 1


def test_persist_end_to_end_records_correct_port(
    isolated_state_db: Path, tmp_path: Path, pg_schema: str
) -> None:
    # Arrange
    from scitex_agent_container._listen._self_peers import discover_self_peers

    _write_self_spec(
        tmp_path, "listen_url: http://127.0.0.1:8181\n", dirname="capsule-9"
    )
    peers = discover_self_peers([tmp_path], self_identity=None)
    # Act
    persist_discovered_self_peers(
        peers, canonical_host="e2e-host"
    )
    # Assert
    assert _fetch_comms_node_port("capsule-9") == 8181
