"""Tests for ``_listen._self_peer_persistence`` (Q4: persist discovered
self-peers into ``comms_nodes`` so the federated graph survives a
listen restart).

Lead dispatch a2a c8b64f298b8a...: the channel-side
``_channel_self_register`` already UPSERTs a row for the running
``sac mcp channel`` session. Q4 mirrors that on the listen side —
every self-peer the cwd-walk discovers (``agents/self/spec.yaml``
under any search dir) lands in ``comms_nodes`` at listen startup so
``sac a2a peers`` keeps reporting them across reboots.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — a real throwaway
PostgreSQL schema (``pg_schema``) and a real ``discover_self_peers`` walk
where it matters.

THE ``isolated_state_db`` FIXTURE AND THE THREE RAW-SQL HELPERS ARE GONE
(2026-08-28). comms_nodes moved to PostgreSQL, so the fixture isolated a
file this module no longer writes, and the helpers
(``SELECT ... FROM comms_nodes``) read a table that is never populated
again — they would have returned 0 for every assertion, i.e. reported
"nothing was persisted" no matter what the code did. The replacements
below ask the same questions through the module's real read surface
(``lookup_comms_node`` / ``list_comms_nodes``), which is what production
reads too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen._self_peer_persistence import (
    persist_discovered_self_peers,
)
from scitex_agent_container._state.state_db_nodes import (
    CommsNodeConflictError,
    register_comms_node,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _count_comms_nodes(name: str | None = None) -> int:
    """Helper — count records, optionally filtered by name."""
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    rows = list_comms_nodes()
    if name is None:
        return len(rows)
    return len([r for r in rows if r["name"] == name])


def _fetch_comms_node_port(name: str) -> int | None:
    """Helper — return ``a2a_port`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    row = lookup_comms_node(name=name)
    return int(row["a2a_port"]) if row is not None else None


def _fetch_comms_node_host(name: str) -> str | None:
    """Helper — return ``host`` for ``name`` or None if absent."""
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    row = lookup_comms_node(name=name)
    return str(row["host"]) if row is not None else None


# ---------------------------------------------------------------------------
# persist_discovered_self_peers — happy path
# ---------------------------------------------------------------------------


def test_persist_writes_a_row_for_a_discovered_self_peer(
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


def test_persist_returns_count_of_written_rows(pg_schema: str) -> None:
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


def test_persist_skips_peer_with_empty_listen_url(pg_schema: str) -> None:
    # Arrange
    peers = [{"name": "ghost", "listen_url": ""}]
    # Act
    written = persist_discovered_self_peers(
        peers, canonical_host="test-host"
    )
    # Assert
    assert written == 0


def test_persist_skips_peer_with_zero_port(pg_schema: str) -> None:
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


def test_persist_skips_peer_with_empty_name(pg_schema: str) -> None:
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
    pg_schema: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — pre-existing row from a DIFFERENT source_host claims the name.
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


def test_persist_does_not_raise_on_conflict(pg_schema: str) -> None:
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
    pg_schema: str, tmp_path: Path
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
    pg_schema: str, tmp_path: Path
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
