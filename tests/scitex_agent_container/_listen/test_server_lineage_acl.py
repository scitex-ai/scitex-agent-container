"""Wired tests for PR-3 lineage-scoped ACL on DELETE + tail.

The new ACL surface plumbing in :mod:`._listen.server.agent_delete`
and :mod:`._listen._tail.agent_tail` runs through the real Starlette
``TestClient`` + a per-node bearer token mapping, so:

  * a non-host-bearer caller with no lineage edge to ``<name>`` lands
    on 403 + ``kind="acl_deny"`` + the structured deny body;
  * a host-bearer caller (admin) keeps the previous behaviour
    (200 / 404 / 410) — no regression on the operator path;
  * a per-node bearer for a caller WITH a lineage edge to ``<name>``
    also passes (descendant control allowed).

No mocks (PA-306); AAA + one assert (PA-307). The node tokens
table is seeded via :func:`mint_node_token` (the real persistence
path the listen server reads at request time).
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
    record_lineage,
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
    """Mint a per-node bearer for ``name`` and return the matching
    Authorization header. Uses real persistence — the listen app
    resolves the bearer back to ``name`` via NodeAuthMiddleware."""
    token = mint_node_token(name=name)
    return {"authorization": f"Bearer {token}"}


def _host_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {HOST_TOKEN}"}


# ---------------------------------------------------------------------------
# DELETE — admin path (host bearer) is always allowed
# ---------------------------------------------------------------------------


def test_delete_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — host bearer is the admin path; ACL gate must allow.
    # The target ``ghost`` doesn't exist so we still get a 404, but
    # the gate must NOT be the failure cause.
    # Act
    response = client.delete("/agents/ghost", headers=_host_headers())
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# DELETE — non-host bearer w/ no lineage → 403 acl_deny
# ---------------------------------------------------------------------------


def test_delete_unrelated_caller_returns_403(client, isolated_env):
    # Arrange — alice is a known node with no lineage edge to
    # ``unrelated-target``.
    headers = _node_headers("alice")
    # Act
    response = client.delete("/agents/unrelated-target", headers=headers)
    # Assert
    assert response.status_code == 403


def test_delete_unrelated_caller_body_has_kind_acl_deny(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.delete("/agents/unrelated-target-2", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["kind"] == "acl_deny"


def test_delete_unrelated_caller_body_has_error_acl_deny(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.delete("/agents/unrelated-target-3", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["error"] == "ACL deny"


def test_delete_unrelated_caller_body_has_reason_naming_target(client, isolated_env):
    # Arrange — the deny reason must name the target for diagnosability.
    headers = _node_headers("alice")
    # Act
    response = client.delete("/agents/unrelated-target-4", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert "unrelated-target-4" in body["reason"]


# ---------------------------------------------------------------------------
# DELETE — caller managing self / descendant → ACL allows
# ---------------------------------------------------------------------------


def test_delete_caller_can_target_self(client, isolated_env):
    # Arrange — self-management is always allowed by the lineage
    # gate. The target ``alice`` itself has no pid file so we
    # still get 404 from absence, but NOT 403 from ACL.
    headers = _node_headers("alice")
    # Act
    response = client.delete("/agents/alice", headers=headers)
    # Assert
    assert response.status_code != 403


def test_delete_caller_can_target_direct_child(client, isolated_env):
    # Arrange — alice has child ``kid`` via the lineage table.
    headers = _node_headers("alice")
    record_lineage(child="kid", parent="alice")
    # Act
    response = client.delete("/agents/kid", headers=headers)
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# tail — admin path
# ---------------------------------------------------------------------------


def test_tail_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — admin path; ACL allows. Target has no session.jsonl
    # so tail returns 404 without follow, but NOT 403.
    # Act
    response = client.get("/agents/ghost/tail", headers=_host_headers())
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# tail — non-host bearer w/ no lineage → 403 acl_deny
# ---------------------------------------------------------------------------


def test_tail_unrelated_caller_returns_403(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.get("/agents/unrelated-target/tail", headers=headers)
    # Assert
    assert response.status_code == 403


def test_tail_unrelated_caller_body_has_kind_acl_deny(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.get("/agents/unrelated-target-2/tail", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# tail — caller managing self / descendant → ACL allows
# ---------------------------------------------------------------------------


def test_tail_caller_can_target_self(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.get("/agents/alice/tail", headers=headers)
    # Assert
    assert response.status_code != 403


def test_tail_caller_can_target_descendant(client, isolated_env):
    # Arrange — root → alice → ada (grandchild).
    headers = _node_headers("root")
    record_lineage(child="alice", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    response = client.get("/agents/ada/tail", headers=headers)
    # Assert
    assert response.status_code != 403
