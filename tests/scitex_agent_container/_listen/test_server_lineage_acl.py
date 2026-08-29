"""Wired tests for PR-3 lineage-scoped ACL on DELETE + tail.

The ACL surface plumbing in :mod:`._listen._agent_delete` and
:mod:`._listen._tail.agent_tail` runs through the real Starlette
``TestClient``, so:

  * a host-bearer caller (admin) keeps the previous behaviour
    (200 / 404 / 410) — no regression on the operator path;
  * a bearer that is not the host token never reaches the gate at all —
    the perimeter 403s it first;
  * the gate's own decisions (deny for an unrelated caller, allow for
    self and for a descendant) are asserted directly against
    :func:`check_lineage_acl`, with the deny body shape asserted against
    :func:`deny_response`.

WHY THE DENY CASES ARE NOT DRIVEN OVER HTTP HERE (changed 2026-08-28).
They used to be: ``_node_headers`` minted a per-node bearer with
``mint_node_token`` and the server resolved it back to a caller name, so
``client.delete("/agents/unrelated-target")`` produced a NON-admin caller
and a 403. That feature is gone — nothing in ``src/`` ever minted a
token, ``node_tokens`` held 0 rows on every fleet host, and the resolver
middleware could only ever tag ``authenticated_node = None``.

The consequence is worth stating rather than hiding, because deleting
the feature is what made it legible: ``agent_delete`` and ``agent_tail``
read their caller ONLY from ``request.state.authenticated_node`` — no
body, no query parameter — so over HTTP the caller is ``None`` on every
request and the lineage gate admits it as administrative. That was
equally true before this file changed; the minted bearer was a fixture
that existed nowhere but here, and it made these routes look gated
against a caller shape production has never produced. The gate logic is
still real and still worth covering, so it is covered where it can be
exercised honestly: at the function.

No mocks (PA-306); AAA + one assert (PA-307).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._acl import check_lineage_acl, deny_response
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._state.state_db_nodes import record_lineage

HOST_TOKEN = "host-token-lineage-acl"


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
    # Reload on the FAR side of the env restore — see
    # test_server_startup_failed.py for why popping-then-reloading here
    # silently re-pinned this constant at the operator's real $HOME.
    env_save_restore.reload_after_restore(ss)
    yield tmp_path


@pytest.fixture
def db_path(isolated_env) -> Path:
    """The state.db path ``isolated_env`` pointed the env at."""
    db = isolated_env / "state.db"
    return db


@pytest.fixture
def client(isolated_env):
    app = create_app(token=HOST_TOKEN)
    with TestClient(app) as c:
        yield c


def _host_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {HOST_TOKEN}"}


# ---------------------------------------------------------------------------
# HTTP — the host bearer is the admin path and is never ACL-denied
# ---------------------------------------------------------------------------


def test_delete_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — host bearer is the admin path; ACL gate must allow.
    # The target ``ghost`` doesn't exist so we still get a 404, but
    # the gate must NOT be the failure cause.
    # Act
    response = client.delete("/agents/ghost", headers=_host_headers())
    # Assert
    assert response.status_code != 403


def test_tail_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — admin path; ACL allows. Target has no session.jsonl
    # so tail returns 404 without follow, but NOT 403.
    # Act
    response = client.get("/agents/ghost/tail", headers=_host_headers())
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# HTTP — a non-host bearer is stopped at the PERIMETER, before the gate
# ---------------------------------------------------------------------------


def test_delete_with_non_host_bearer_is_refused_by_the_perimeter(
    client, isolated_env
):
    # Arrange — the only bearer the daemon admits is the host token.
    headers = {"authorization": "Bearer some-other-bearer"}
    # Act
    response = client.delete("/agents/unrelated-target", headers=headers)
    # Assert
    assert response.status_code == 403


def test_delete_non_host_bearer_403_is_an_auth_refusal_not_an_acl_deny(
    client, isolated_env
):
    """The two 403s are different operator actions, so the bodies differ:
    the perimeter says the bearer is invalid; the gate says ACL deny."""
    # Arrange
    headers = {"authorization": "Bearer some-other-bearer"}
    # Act
    response = client.delete("/agents/unrelated-target", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body == {"error": "invalid bearer token"}


def test_tail_with_non_host_bearer_is_refused_by_the_perimeter(client, isolated_env):
    # Arrange
    headers = {"authorization": "Bearer some-other-bearer"}
    # Act
    response = client.get("/agents/unrelated-target/tail", headers=headers)
    # Assert
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# check_lineage_acl — the gate's decisions, asserted where a non-admin
# caller can still be constructed
# ---------------------------------------------------------------------------


def test_gate_denies_caller_with_no_lineage_edge(pg_schema: str, db_path: Path):
    # Arrange — alice has no lineage edge to ``unrelated-target``.
    caller = "alice"
    # Act
    decision, _reason = check_lineage_acl(
        caller=caller, target="unrelated-target"
    )
    # Assert
    assert decision == "deny"


def test_gate_deny_reason_names_the_target(pg_schema: str, db_path: Path):
    # Arrange — the deny reason must name the target for diagnosability.
    caller = "alice"
    # Act
    _decision, reason = check_lineage_acl(
        caller=caller, target="unrelated-target-4"
    )
    # Assert
    assert reason is not None and "unrelated-target-4" in reason


def test_gate_allows_caller_targeting_self(pg_schema: str, db_path: Path):
    # Arrange — self-management is always allowed.
    caller = "alice"
    # Act
    decision, _reason = check_lineage_acl(
        caller=caller, target="alice"
    )
    # Assert
    assert decision == "allow"


def test_gate_allows_caller_targeting_direct_child(pg_schema: str, db_path: Path):
    # Arrange — alice has child ``kid`` via the lineage table.
    record_lineage(child="kid", parent="alice")
    # Act
    decision, _reason = check_lineage_acl(caller="alice", target="kid")
    # Assert
    assert decision == "allow"


def test_gate_allows_caller_targeting_transitive_descendant(
    pg_schema: str, db_path: Path
):
    # Arrange — root → alice → ada (grandchild).
    record_lineage(child="alice", parent="root")
    record_lineage(child="ada", parent="alice")
    # Act
    decision, _reason = check_lineage_acl(caller="root", target="ada")
    # Assert
    assert decision == "allow"


def test_gate_allows_the_administrative_caller(pg_schema: str, db_path: Path):
    """``caller=None`` — what BOTH HTTP routes above actually pass, on
    every request, now that no per-node identity can be established."""
    # Arrange
    caller = None
    # Act
    decision, _reason = check_lineage_acl(
        caller=caller, target="unrelated-target"
    )
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# deny_response — the wire shape a gate deny produces (5-kind contract)
# ---------------------------------------------------------------------------


def test_deny_response_body_has_kind_acl_deny():
    # Arrange
    response = deny_response("lineage ACL deny: caller 'alice' ...")
    # Act
    body = json.loads(response.body)
    # Assert
    assert body["kind"] == "acl_deny"


def test_deny_response_body_has_error_acl_deny():
    # Arrange
    response = deny_response("lineage ACL deny: caller 'alice' ...")
    # Act
    body = json.loads(response.body)
    # Assert
    assert body["error"] == "ACL deny"


def test_deny_response_body_carries_the_reason_verbatim():
    # Arrange
    reason = "lineage ACL deny: caller 'alice' has no edge to 'unrelated-target-4'"
    response = deny_response(reason)
    # Act
    body = json.loads(response.body)
    # Assert
    assert body["reason"] == reason


def test_deny_response_status_is_403():
    # Arrange
    response = deny_response("lineage ACL deny: caller 'alice' ...")
    # Act
    status = response.status_code
    # Assert
    assert status == 403
