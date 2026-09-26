"""Status must load the exact spec named by the active registry row.

No mocks (PA-306): the authenticated route runs through a real Starlette app,
real registry JSON, and two real same-name specs under ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _session_state
from scitex_agent_container._state import registry as _registry
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_TOKEN = "status-registry-authority-token"
_NAME = "duplicate-agent"


def _write_spec(path: Path, *, workdir: str, role: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": role}},
        "spec": explicit_spec(
            {
                "runtime": "tui",
                "host": "${HOSTNAME}",
                "workdir": workdir,
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "claude-sonnet-4-5"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            }
        ),
    }
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


@pytest.fixture
def status_case(tmp_path: Path, env_save_restore):
    """A stale search-tree spec and a different active registry spec."""
    saved_registry_dir = _registry.REGISTRY_DIR
    saved_state_root = _session_state.DEFAULT_STATE_ROOT
    registry_dir = tmp_path / "runtime" / "registry"
    search_root = tmp_path / "search-root"
    active = _write_spec(
        tmp_path / "active-incarnation" / _NAME / "spec.yaml",
        workdir="/work/active-incarnation",
        role="active-role",
    )
    stale = _write_spec(
        search_root / "agent-container" / "agents" / _NAME / "spec.yaml",
        workdir="/work/stale-search-tree",
        role="stale-role",
    )
    env_save_restore.set("SCITEX_DIR", str(search_root))
    env_save_restore.set("SAC_AGENT_SCOPE", "user")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(registry_dir))
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    _registry.REGISTRY_DIR = registry_dir
    _session_state.DEFAULT_STATE_ROOT = tmp_path / "runtime" / "agents"
    _registry.Registry().add(_NAME, str(active), f"tui-{_NAME}", pid=os.getpid())
    try:
        with TestClient(create_app(token=_TOKEN)) as client:
            yield client, active, stale
    finally:
        _registry.REGISTRY_DIR = saved_registry_dir
        _session_state.DEFAULT_STATE_ROOT = saved_state_root


def _get(client: TestClient):
    return client.get(
        f"/agents/{_NAME}/status",
        headers={"authorization": f"Bearer {_TOKEN}"},
    )


def test_status_reports_the_active_registry_spec_path(status_case) -> None:
    # Arrange
    client, active, _stale = status_case
    # Act
    body = _get(client).json()
    # Assert
    assert body["spec_path"] == str(active)


def test_status_loads_workdir_from_active_registry_spec(status_case) -> None:
    # Arrange
    client, _active, _stale = status_case
    # Act
    body = _get(client).json()
    # Assert
    assert body["workdir"] == "/work/active-incarnation"


def test_status_identity_does_not_re_resolve_the_stale_name(status_case) -> None:
    # Arrange
    client, _active, _stale = status_case
    # Act
    body = _get(client).json()
    # Assert
    assert body["role"] == "active-role"


def test_missing_registered_spec_is_unknown_not_unknown_agent(status_case) -> None:
    # Arrange: leave the stale searchable spec intact but remove the spec the
    # active registry incarnation explicitly owns.
    client, active, _stale = status_case
    active.unlink()
    # Act
    response = _get(client)
    # Assert
    assert (response.status_code, response.json()["kind"]) == (
        500,
        "spec_unreadable",
    )
