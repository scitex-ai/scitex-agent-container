"""A2A v1.0 well-known agent-card path coverage for ``_listen.server``.

Confirms the listen surface uses the canonical
``/.well-known/agent-card.json`` paths (per-agent + fleet) instead of
the pre-v1 ``/agents/<name>/card`` route. Per ADR-0004 the old route
is dropped outright — no compat alias.

Drives the real Starlette app through
:class:`starlette.testclient.TestClient`. No mocks / monkeypatch — the
on-disk roots (registry / runtime / ``HOME``) are re-pointed at the
per-test ``tmp_path`` via fixture parameters (configuration, not
mocking).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg

TOKEN = "test-token-wkp"

WELL_KNOWN = ".well-known/agent-card.json"
OLD_CARD_SUFFIX = "card"


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Point every on-disk root at ``tmp_path`` for the duration of one test."""
    saved_home = os.environ.get("HOME")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_yaml_env = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"

    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, val in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml_env),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.fixture
def client(isolated_env):
    """Real Starlette TestClient against ``create_app`` with bearer auth."""
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_spec(home: Path, name: str, workdir: str = "/tmp") -> Path:
    """Write a minimal v3 spec.yaml that ``load_config`` will accept."""
    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "claude": {"model": "claude-sonnet-4-5"},
            "workdir": workdir,
        },
    }
    p = spec_dir / "spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


# --- Per-agent well-known path --------------------------------------------


class TestPerAgentWellKnownPath:
    def test_per_agent_card_route_is_well_known_path(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        _write_spec(isolated_env, "alpha")
        # Act
        resp = client.get(f"/agents/alpha/{WELL_KNOWN}", headers=auth_headers)
        # Assert
        assert resp.status_code == 200

    def test_old_card_route_returns_404(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "alpha")
        # Act
        resp = client.get(f"/agents/alpha/{OLD_CARD_SUFFIX}", headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_per_agent_card_requires_auth(self, client, isolated_env):
        # Arrange
        _write_spec(isolated_env, "alpha")
        # Act
        resp = client.get(f"/agents/alpha/{WELL_KNOWN}")
        # Assert
        assert resp.status_code == 401


# --- Fleet well-known path ------------------------------------------------


class TestFleetWellKnownPath:
    def test_fleet_card_route_exists(self, client, auth_headers):
        # Arrange
        url = f"/{WELL_KNOWN}"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 200

    def test_fleet_card_lists_known_agents(self, client, auth_headers, isolated_env):
        # Arrange
        r = _reg.Registry()
        r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
        r.add("beta", "/path/to/spec.yaml", "sc-beta", pid=12346)
        # Act
        body = client.get(f"/{WELL_KNOWN}", headers=auth_headers).json()
        # Assert
        members = body["x-scitex-agent-container"]["agents"]
        assert {m["name"] for m in members} == {"alpha", "beta"}

    def test_fleet_card_requires_auth(self, client):
        # Arrange
        url = f"/{WELL_KNOWN}"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.status_code == 401
