"""Wired tests for the POST /agents/<name>/restart route ACL gate.

The container-side restart bypass (mirror of the spawn bypass) routes
through the real Starlette ``TestClient`` + a per-node bearer mapping,
exactly like :mod:`test_server_lineage_acl` does for DELETE / tail:

  * a non-host-bearer caller with no lineage edge AND no group mesh to
    ``<name>`` lands on 403 + ``kind="acl_deny"``;
  * a host-bearer caller (admin) is NOT blocked by the gate (it shells
    the bare-host restart, which fails to resolve the ghost agent — but
    the ACL gate is not the failure cause).

These assert the gate is wired on the new route; the host-shell leg
(``sac agents restart``) is exercised separately by the CLI tests.

No mocks (PA-306); AAA + one assert (PA-307). Node tokens seeded via
:func:`mint_node_token` (the real persistence path the server reads).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._state.state_db_nodes import (
    mint_node_token,
    record_comms_policy,
)

HOST_TOKEN = "test-host-bearer"


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    yaml_dir = home / ".scitex" / "agent-container" / "agents"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    state_db_path = tmp_path / "state.db"
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(state_db_path))
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    yield tmp_path
    os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
    os.environ.pop("HOME", None)
    importlib.reload(ss)


@pytest.fixture
def client(isolated_env):
    app = create_app(token=HOST_TOKEN)
    with TestClient(app) as c:
        yield c


def _node_headers(name: str) -> dict[str, str]:
    token = mint_node_token(name=name)
    return {"authorization": f"Bearer {token}"}


def _host_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {HOST_TOKEN}"}


# ---------------------------------------------------------------------------
# restart — non-host bearer w/ no lineage / no mesh → 403 acl_deny
# ---------------------------------------------------------------------------


def test_restart_unrelated_caller_returns_403(client, isolated_env):
    # Arrange — alice has no lineage edge and no group mesh to the target.
    headers = _node_headers("alice")
    # Act
    response = client.post("/agents/unrelated-target/restart", headers=headers)
    # Assert
    assert response.status_code == 403


def test_restart_unrelated_caller_body_has_kind_acl_deny(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.post("/agents/unrelated-target-2/restart", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# restart — host bearer (admin) is NOT blocked by the ACL gate
# ---------------------------------------------------------------------------


def test_restart_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — host bearer is the admin path; the gate must allow even
    # though the ghost agent has no row (the bare-host shell will fail,
    # but NOT with a 403 from the ACL gate).
    # Act
    response = client.post("/agents/ghost/restart", headers=_host_headers())
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# restart — standard-fleet mesh: a researcher may manage a developer peer
# ---------------------------------------------------------------------------


def test_restart_researcher_to_developer_not_403(client, isolated_env):
    # Arrange — neurovista (researcher) restarts scitex-todo (developer):
    # the manage mesh allows it with no lineage edge. The target has no
    # row so the bare-host shell fails, but NOT via a 403 ACL deny.
    record_comms_policy(name="neurovista", group_name="researcher")
    record_comms_policy(name="scitex-todo", group_name="developer")
    headers = _node_headers("neurovista")
    # Act
    response = client.post("/agents/scitex-todo/restart", headers=headers)
    # Assert
    assert response.status_code != 403
