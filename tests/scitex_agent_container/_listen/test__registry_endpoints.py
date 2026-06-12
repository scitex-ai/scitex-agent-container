"""Tests for ``_listen._registry_endpoints`` (Q1: a2a_port + turn_url enrichment).

Per the lead dispatch a2a dc6fd23387f64e329049d218cf85a4d4: ``GET /agents``
and ``GET /agents/<name>/status`` need to surface the bound A2A port and
the derived ``/v1/turn`` URL on every row so scitex-todo's notify
resolver (P3a-b) can dispatch nudge→turn without redeploying.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — uses real state.db
under ``tmp_path`` plus ``monkeypatch`` to redirect the module-level
``DEFAULT_DB_PATH`` (same pattern as ``test_server.py``'s
``cross_host_env`` fixture).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen import _registry_endpoints as _re
from scitex_agent_container._state import port_allocator as _pa
from scitex_agent_container._state import state_db as _state_db
from scitex_agent_container._state import state_db_instances as _instances

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``state.db`` writes to a per-test tmp file.

    Mirrors the ``cross_host_env`` fixture in ``test_server.py``:
    ``DEFAULT_DB_PATH`` is captured at import time so just setting the
    env var is not enough — we patch the module attribute directly.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(db))
    monkeypatch.setattr(_state_db, "DEFAULT_DB_PATH", db)
    _state_db.init_schema(db)
    return db


@pytest.fixture
def isolated_host_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin host_config + state.db to ``tmp_path`` so canonical_host is stable."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(db))
    monkeypatch.setattr(_state_db, "DEFAULT_DB_PATH", db)
    _state_db.init_schema(db)
    # Pin the canonical hostname so ``resolve_a2a_host``'s local fallback
    # is deterministic.
    monkeypatch.setenv("SAC_HOST", "test-host")
    return db


# ---------------------------------------------------------------------------
# resolve_a2a_port
# ---------------------------------------------------------------------------


def test_resolve_a2a_port_returns_allocator_value_when_present(
    isolated_state_db: Path,
) -> None:
    # Arrange
    _pa.claim_port("alpha", range_=(20000, 20001), db_path=isolated_state_db)
    # Act
    result = _re.resolve_a2a_port("alpha")
    # Assert
    assert result == 20000


def test_resolve_a2a_port_falls_back_to_instance_when_allocator_empty(
    isolated_state_db: Path,
) -> None:
    # Arrange — no port_allocator claim; an instance row holds the port.
    _instances.record_instance_start(
        name="beta", host="other-host", a2a_port=31337, db_path=isolated_state_db
    )
    # Act
    result = _re.resolve_a2a_port("beta")
    # Assert
    assert result == 31337


def test_resolve_a2a_port_returns_none_when_both_sources_empty(
    isolated_state_db: Path,
) -> None:
    # Arrange — empty db.
    name = "ghost"
    # Act
    result = _re.resolve_a2a_port(name)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# resolve_a2a_host
# ---------------------------------------------------------------------------


def test_resolve_a2a_host_returns_instance_host_when_set(
    isolated_state_db: Path,
) -> None:
    # Arrange — cross-host instance row pinned to a non-local host.
    _instances.record_instance_start(
        name="gamma",
        host="other-host",
        a2a_port=40404,
        db_path=isolated_state_db,
    )
    # Act
    result = _re.resolve_a2a_host("gamma")
    # Assert
    assert result == "other-host"


def test_resolve_a2a_host_falls_back_to_canonical_host_when_no_instance(
    isolated_host_env: Path,
) -> None:
    # Arrange — no instance row; canonical_host comes from $SAC_HOST.
    name = "delta"
    # Act
    result = _re.resolve_a2a_host(name)
    # Assert
    assert result == "test-host"


# ---------------------------------------------------------------------------
# derive_turn_url
# ---------------------------------------------------------------------------


def test_derive_turn_url_returns_full_url_when_inputs_valid() -> None:
    # Arrange
    host = "node-7"
    port = 19042
    # Act
    result = _re.derive_turn_url(host, port)
    # Assert
    assert result == "http://node-7:19042/v1/turn"


def test_derive_turn_url_returns_none_when_port_is_none() -> None:
    # Arrange
    host = "node-7"
    port = None
    # Act
    result = _re.derive_turn_url(host, port)
    # Assert
    assert result is None


def test_derive_turn_url_returns_none_when_host_is_none() -> None:
    # Arrange
    host = None
    port = 19042
    # Act
    result = _re.derive_turn_url(host, port)
    # Assert
    assert result is None


def test_derive_turn_url_returns_none_when_host_is_empty_string() -> None:
    # Arrange
    host = ""
    port = 19042
    # Act
    result = _re.derive_turn_url(host, port)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# enrich_row_with_endpoint
# ---------------------------------------------------------------------------


def test_enrich_row_with_endpoint_adds_both_fields_when_sources_present(
    isolated_host_env: Path,
) -> None:
    # Arrange
    _pa.claim_port("epsilon", range_=(21000, 21001), db_path=isolated_host_env)
    row = {"name": "epsilon"}
    # Act
    enriched = _re.enrich_row_with_endpoint(row)
    # Assert
    assert enriched["turn_url"] == "http://test-host:21000/v1/turn"


def test_enrich_row_with_endpoint_carries_resolved_a2a_port(
    isolated_host_env: Path,
) -> None:
    # Arrange
    _pa.claim_port("zeta", range_=(21100, 21101), db_path=isolated_host_env)
    row = {"name": "zeta"}
    # Act
    enriched = _re.enrich_row_with_endpoint(row)
    # Assert
    assert enriched["a2a_port"] == 21100


def test_enrich_row_with_endpoint_preserves_preexisting_a2a_port(
    isolated_host_env: Path,
) -> None:
    # Arrange — self-peer-style row already carrying its own port; the
    # allocator's claim for the SAME name must NOT clobber it.
    _pa.claim_port("eta", range_=(21200, 21201), db_path=isolated_host_env)
    row = {"name": "eta", "a2a_port": 9999, "turn_url": None}
    # Act
    enriched = _re.enrich_row_with_endpoint(row)
    # Assert
    assert enriched["a2a_port"] == 9999


def test_enrich_row_with_endpoint_preserves_preexisting_turn_url(
    isolated_host_env: Path,
) -> None:
    # Arrange — self-peer-style row already carrying its own turn_url.
    _pa.claim_port("theta", range_=(21300, 21301), db_path=isolated_host_env)
    row = {
        "name": "theta",
        "a2a_port": None,
        "turn_url": "http://explicit:1234/v1/turn",
    }
    # Act
    enriched = _re.enrich_row_with_endpoint(row)
    # Assert
    assert enriched["turn_url"] == "http://explicit:1234/v1/turn"
