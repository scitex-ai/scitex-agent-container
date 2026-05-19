"""WI-2 — ACL gate on ``message:send`` + spawn-permission gate
(limited scope per lead 2026-05-20).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2):

  Acceptance (limited scope): an un-permitted (cross-group, no
  grant) sender is rejected with ``403`` + a log line; an
  intra-group sender's message lands; a child's
  ``sac agents start`` is rejected.

The "identity cannot be spoofed via a metadata field" acceptance
criterion is DEFERRED (lead 2026-05-20) to a separate follow-on
handoff. Until then, the ACL gates on the self-claimed
``metadata.from_agent`` field and every cross-group grant carries
the audit caveat "trusts metadata.from_agent until per-node creds
land".

Mirrors ``src/scitex_agent_container/_listen/_acl.py``. No mocks
(handoff §0): real SQLite, real Starlette app.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._acl import check_send_acl, check_spawn
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import state_db
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    record_lineage,
)


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db. PA-306 no-mocks: yield-based env override."""
    # Arrange
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


# ---------------------------------------------------------------------------
# Unit-level: check_send_acl decisions
# ---------------------------------------------------------------------------


def test_acl_allows_self_send(db_path: Path) -> None:
    """A node may always address itself — trivial allow."""
    # Arrange
    sender = "alice"
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent=sender, target="alice", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_parent_to_child(db_path: Path) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="root", target="worker-a", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_sibling_to_sibling(db_path: Path) -> None:
    """Handoff §4: 'parent↔child *and* sibling↔sibling, bidirectional'."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="worker-a", target="worker-b", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_acl_denies_cross_group_without_grant(db_path: Path) -> None:
    # Arrange — two unrelated families
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="child-1", target="child-2", db_path=db_path
    )
    # Assert
    assert decision == "deny"


def test_acl_deny_carries_explanatory_reason(db_path: Path) -> None:
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    _decision, reason = check_send_acl(
        claimed_from_agent="child-1", target="child-2", db_path=db_path
    )
    # Assert
    assert reason is not None and "cross-group" in reason


def test_acl_allows_cross_group_with_explicit_grant(db_path: Path) -> None:
    """Explicit cross-group grant flips a deny to allow."""
    # Arrange — two unrelated families + grant child-1 → child-2
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    grant_send(sender="child-1", target="child-2", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="child-1", target="child-2", db_path=db_path
    )
    # Assert
    assert decision == "allow"


def test_acl_denies_when_metadata_from_agent_missing(db_path: Path) -> None:
    """Empty sender → deny. No identity to gate on."""
    # Arrange
    target = "anyone"
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent=None, target=target, db_path=db_path
    )
    # Assert
    assert decision == "deny"


def test_acl_denies_when_target_missing(db_path: Path) -> None:
    # Arrange
    sender = "alice"
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent=sender, target="", db_path=db_path
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# Spawn-permission gate (check_spawn / spawn_allowed)
# ---------------------------------------------------------------------------


def test_spawn_allows_root_caller(db_path: Path) -> None:
    """A node with no parent in lineage is allowed to spawn."""
    # Arrange
    caller = "root"
    # Act
    decision, _reason = check_spawn(caller=caller, db_path=db_path)
    # Assert
    assert decision == "allow"


def test_spawn_allows_admin_caller_when_caller_is_none(db_path: Path) -> None:
    """``caller=None`` is the administrative / operator path."""
    # Arrange
    caller = None
    # Act
    decision, _reason = check_spawn(caller=caller, db_path=db_path)
    # Assert
    assert decision == "allow"


def test_spawn_denies_child_caller(db_path: Path) -> None:
    """A node with a parent (child) is NOT allowed to spawn."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="worker-a", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_spawn_deny_reason_explains_root_only_policy(db_path: Path) -> None:
    """The 403 body explains the lift-able policy."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    _decision, reason = check_spawn(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "lift-able policy" in reason


# ---------------------------------------------------------------------------
# HTTP-level: node_message_send returns 403 on cross-group deny.
# ---------------------------------------------------------------------------


TOKEN = "test-token-acl"


@pytest.fixture
def isolated_listen_env(tmp_path: Path, db_path: Path):
    """Point Registry / runtime dirs at tmp_path; reuse db_path fixture."""
    # Arrange
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _payload(sender: str, content: str = "x") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": {"from_agent": sender},
        },
    }


def test_http_node_message_send_denies_cross_group_with_403(
    isolated_listen_env, db_path: Path
) -> None:
    """End-to-end: cross-group sender → 403."""
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert
    assert r.status_code == 403, r.text


def test_http_node_message_send_403_body_carries_reason(
    isolated_listen_env, db_path: Path
) -> None:
    """The 403 body explains the denial."""
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    body = r.json()
    # Assert
    assert "reason" in body and "cross-group" in body["reason"]


def test_http_node_message_send_allows_intra_group(
    isolated_listen_env, db_path: Path
) -> None:
    """Intra-group send (sibling-to-sibling) lands."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert
    assert r.status_code < 400, r.text


def test_http_node_message_send_allows_after_explicit_grant(
    isolated_listen_env, db_path: Path
) -> None:
    """A cross-group grant flips the deny to an allow."""
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    grant_send(sender="child-1", target="child-2", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert
    assert r.status_code < 400, r.text


# ---------------------------------------------------------------------------
# HTTP-level: agents_start denies a child caller with 403.
# ---------------------------------------------------------------------------


def test_http_agents_start_denies_child_caller_with_403(
    isolated_listen_env, db_path: Path
) -> None:
    """Root-only spawn (current policy): a child caller → 403."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    app = create_app(token=TOKEN)
    body = {"name": "new-agent", "caller": "worker-a"}
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents",
            json=body,
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert
    assert r.status_code == 403, r.text


def test_http_agents_start_403_carries_lift_able_policy_text(
    isolated_listen_env, db_path: Path
) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    app = create_app(token=TOKEN)
    body = {"name": "new-agent", "caller": "worker-a"}
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents",
            json=body,
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    body_json = r.json()
    # Assert
    assert "lift-able policy" in body_json.get("reason", "")
