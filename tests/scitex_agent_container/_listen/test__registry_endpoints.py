"""Tests for ``_listen._registry_endpoints`` (Q1: a2a_port + turn_url enrichment).

Per the lead dispatch a2a dc6fd23387f64e329049d218cf85a4d4: ``GET /agents``
and ``GET /agents/<name>/status`` need to surface the bound A2A port and
the derived ``/v1/turn`` URL on every row so scitex-todo's notify
resolver (P3a-b) can dispatch nudge→turn without redeploying.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — uses real state.db
under ``tmp_path`` plus a hand-rolled yield-fixture that swaps the
process-level state-db env var + module attribute and restores both
on teardown (same effect as the ``cross_host_env`` fixture in
``test_server.py``, without the ``monkeypatch`` parameter that
PA-306 §3 forbids).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._listen import _registry_endpoints as _re
from scitex_agent_container._state import port_allocator as _pa
from scitex_agent_container._state import state_db as _state_db
from scitex_agent_container._state import state_db_instances as _instances

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_STATE_DB_ENV = "SCITEX_AGENT_CONTAINER_STATE_DB"
_SAC_HOST_ENV = "SAC_HOST"


def _swap_env(name: str, value: str | None) -> str | None:
    """Set or unset env var ``name`` to ``value``; return the prior value.

    No ``monkeypatch`` dependency — the fixtures below pair this with a
    matching restore call in a ``finally`` block.
    """
    prev = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return prev


@pytest.fixture
def isolated_state_db(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``state.db`` writes to a per-test tmp file.

    Mirrors the ``cross_host_env`` fixture in ``test_server.py``:
    ``DEFAULT_DB_PATH`` is captured at import time so just setting the
    env var is not enough — we swap the module attribute directly and
    restore it on teardown.
    """
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


@pytest.fixture
def isolated_host_env(tmp_path: Path) -> Iterator[Path]:
    """Pin host_config + state.db to ``tmp_path`` so canonical_host is stable."""
    db = tmp_path / "state.db"
    prev_db_env = _swap_env(_STATE_DB_ENV, str(db))
    prev_host_env = _swap_env(_SAC_HOST_ENV, "test-host")
    prev_attr = _state_db.DEFAULT_DB_PATH
    _state_db.DEFAULT_DB_PATH = db
    _state_db.init_schema(db)
    try:
        yield db
    finally:
        _state_db.DEFAULT_DB_PATH = prev_attr
        _swap_env(_STATE_DB_ENV, prev_db_env)
        _swap_env(_SAC_HOST_ENV, prev_host_env)


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


# ---------------------------------------------------------------------------
# enrich_row_with_role_owner — role + owned project on every peers row
# (operator 2026-07-03: ownership discoverable fleet-wide via a2a peers)
# ---------------------------------------------------------------------------


def _fixed_resolver(role, project):
    """Hand-rolled (not a mock) resolver stub for the pure enrichment logic."""

    def _resolve(_name: str):
        return (role, project)

    return _resolve


def test_enrich_row_with_role_owner_adds_role_from_resolver() -> None:
    # Arrange
    row = {"name": "scitex-dev"}
    # Act
    enriched = _re.enrich_row_with_role_owner(
        row, resolver=_fixed_resolver("project-maintainer", "scitex")
    )
    # Assert
    assert enriched["role"] == "project-maintainer"


def test_enrich_row_with_role_owner_adds_project_from_resolver() -> None:
    # Arrange
    row = {"name": "scitex-dev"}
    # Act
    enriched = _re.enrich_row_with_role_owner(
        row, resolver=_fixed_resolver("project-maintainer", "scitex")
    )
    # Assert
    assert enriched["project"] == "scitex"


def test_enrich_row_with_role_owner_preserves_preexisting_role() -> None:
    # Arrange — a row that already carries its own role must not be clobbered.
    row = {"name": "scitex-dev", "role": "explicit-role"}
    # Act
    enriched = _re.enrich_row_with_role_owner(
        row, resolver=_fixed_resolver("resolved-role", "resolved-proj")
    )
    # Assert
    assert enriched["role"] == "explicit-role"


def test_enrich_row_with_role_owner_preserves_preexisting_project() -> None:
    # Arrange
    row = {"name": "scitex-dev", "project": "explicit-proj"}
    # Act
    enriched = _re.enrich_row_with_role_owner(
        row, resolver=_fixed_resolver("resolved-role", "resolved-proj")
    )
    # Assert
    assert enriched["project"] == "explicit-proj"


def test_enrich_row_with_role_owner_nameless_row_gets_null_fields() -> None:
    # Arrange — a row with no usable name still gets a uniform shape.
    row = {"kind": "comms-node"}
    # Act
    enriched = _re.enrich_row_with_role_owner(
        row, resolver=_fixed_resolver("x", "y")
    )
    # Assert
    assert enriched["role"] is None and enriched["project"] is None


def test_resolve_role_and_project_unknown_agent_returns_none_none(
    isolated_state_db: Path,
) -> None:
    # Arrange — a name with no resolvable spec degrades to (None, None).
    name = "no-such-agent-xyz"
    # Act
    result = _re.resolve_role_and_project(name)
    # Assert
    assert result == (None, None)


@pytest.fixture
def spec_on_yaml_path(tmp_path: Path) -> Iterator[str]:
    """Write a real v3 spec with a role + workdir and expose it via
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` so ``resolve_config`` finds it."""
    import yaml

    name = "probe-owner"
    spec_dir = tmp_path / "yaml_dirs"
    p = spec_dir / name / "spec.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": "project-maintainer"}},
        "spec": {
            "runtime": "apptainer",
            "host": "local",
            "workdir": "/home/agent/repos/owned-repo",
            "apptainer": {"image": "x.sif", "binds": []},
            "claude": {"model": "sonnet"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        },
    }
    p.write_text(yaml.safe_dump(body))
    prev = _swap_env("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(spec_dir))
    try:
        yield name
    finally:
        _swap_env("SCITEX_AGENT_CONTAINER_YAML_DIRS", prev)


def test_resolve_role_and_project_reads_role_from_spec(
    spec_on_yaml_path: str,
) -> None:
    # Arrange
    name = spec_on_yaml_path
    # Act
    role, _project = _re.resolve_role_and_project(name)
    # Assert
    assert role == "project-maintainer"


def test_resolve_role_and_project_reads_project_from_workdir_basename(
    spec_on_yaml_path: str,
) -> None:
    # Arrange — project is the basename of the agent's resolved workdir.
    name = spec_on_yaml_path
    # Act
    _role, project = _re.resolve_role_and_project(name)
    # Assert
    assert project == "owned-repo"
