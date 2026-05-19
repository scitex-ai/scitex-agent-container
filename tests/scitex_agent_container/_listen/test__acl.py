"""WI-2 — ACL gate on ``message:send`` (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2 "ACL: permissioned
messaging"):

  Acceptance: an un-permitted (cross-group, no grant) sender is
  rejected with ``403`` + a log line; an intra-group sender's
  message lands; ... identity cannot be spoofed via a metadata field.

Mirrors ``src/scitex_agent_container/_listen/_acl.py``. The tests
drive the ACL function directly (unit-level, deterministic) plus
a couple of HTTP-level shape checks against the real ``sac listen``
Starlette app to confirm the wiring lands the 403 with a clear
reason. No mocks (handoff §0).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._acl import check_send_acl
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import state_db
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state.state_db_nodes import (
    mint_node_token,
    record_lineage,
)


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db. PA-306 no-mocks: yield-based env override."""
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
    """A node may always address itself — the trivial allow case."""
    # Arrange
    mint_node_token(name="alice", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="alice",
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_parent_to_child(db_path: Path) -> None:
    # Arrange
    mint_node_token(name="root", db_path=db_path)
    mint_node_token(name="worker-a", db_path=db_path)
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="root",
        claimed_from_agent="root",
        target="worker-a",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_sibling_to_sibling(db_path: Path) -> None:
    """Handoff §4: 'parent↔child *and* sibling↔sibling, bidirectional'."""
    # Arrange
    mint_node_token(name="root", db_path=db_path)
    mint_node_token(name="worker-a", db_path=db_path)
    mint_node_token(name="worker-b", db_path=db_path)
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="worker-a",
        claimed_from_agent="worker-a",
        target="worker-b",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_denies_cross_group_without_grant(db_path: Path) -> None:
    """Two unrelated families. No grant configured (handoff defers
    explicit grants); cross-group must be denied with a clear reason.
    """
    # Arrange
    for name in ("root-1", "child-1", "root-2", "child-2"):
        mint_node_token(name=name, db_path=db_path)
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="child-1",
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_acl_deny_carries_explanatory_reason(db_path: Path) -> None:
    """Denial is the policy working — but the sender must know why."""
    # Arrange
    for name in ("root-1", "child-1", "root-2", "child-2"):
        mint_node_token(name=name, db_path=db_path)
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    _decision, reason = check_send_acl(
        authenticated_node="child-1",
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert reason is not None and "cross-group" in reason


# ---------------------------------------------------------------------------
# Spoofing: bearer authenticates X but metadata.from_agent claims Y
# ---------------------------------------------------------------------------


def test_acl_denies_identity_spoof_mismatched_from_agent(db_path: Path) -> None:
    """The acceptance criterion: 'identity cannot be spoofed via a
    metadata field'. Bearer says ``alice``, metadata claims ``bob`` —
    deny.
    """
    # Arrange
    mint_node_token(name="alice", db_path=db_path)
    mint_node_token(name="bob", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="bob",  # spoof
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_acl_spoof_deny_reason_names_both_identities(db_path: Path) -> None:
    """The 403 body explains *which* identity claimed to be whom."""
    # Arrange
    mint_node_token(name="alice", db_path=db_path)
    mint_node_token(name="bob", db_path=db_path)
    # Act
    _decision, reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="bob",
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert reason is not None and "alice" in reason and "bob" in reason


# ---------------------------------------------------------------------------
# Administrative path: no per-node bearer, host bearer used. Metadata
# must still carry a from_agent so the ACL has something to gate on.
# ---------------------------------------------------------------------------


def test_acl_denies_when_no_identity_at_all(db_path: Path) -> None:
    """Host bearer + empty metadata.from_agent → there is no identity
    to gate on. Deny rather than fall through to an unauthenticated
    send.
    """
    # Arrange
    # (no tokens needed — the denial fires before lineage lookup)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node=None,
        claimed_from_agent=None,
        target="anyone",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_acl_admin_caller_honors_claimed_from_agent(db_path: Path) -> None:
    """Host bearer + metadata.from_agent → trust the claim and run
    the group check against THAT identity. This is the
    cross-host-forwarding path: the forwarding host's bearer
    authenticates the request, but the sender is the original node.
    """
    # Arrange
    mint_node_token(name="root", db_path=db_path)
    mint_node_token(name="worker-a", db_path=db_path)
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act — admin caller (authenticated_node=None) speaks for root
    decision, _reason = check_send_acl(
        authenticated_node=None,
        claimed_from_agent="root",
        target="worker-a",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


# ---------------------------------------------------------------------------
# HTTP-level: the wired-up node_message_send returns 403 on deny.
# ---------------------------------------------------------------------------


TOKEN = "test-token-acl"


@pytest.fixture
def isolated_listen_env(tmp_path: Path, db_path: Path):
    """Point Registry / runtime dirs at tmp_path; reuse db_path fixture."""
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


def _payload(target_sender: str, content: str = "x") -> dict:
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
            "metadata": {"from_agent": target_sender},
        },
    }


def test_http_node_message_send_denies_cross_group_with_403(
    isolated_listen_env, db_path: Path
) -> None:
    """End-to-end: admin caller (host bearer) speaks for a node in
    one group, addresses a node in a different group → 403.
    """
    # Arrange
    for name in ("root-1", "child-1", "root-2", "child-2"):
        mint_node_token(name=name, db_path=db_path)
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
    """The 403 body explains the denial (handoff §4: 'denial is
    explicit ... the sender must know')."""
    # Arrange
    for name in ("root-1", "child-1", "root-2", "child-2"):
        mint_node_token(name=name, db_path=db_path)
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
    for name in ("root", "worker-a", "worker-b"):
        mint_node_token(name=name, db_path=db_path)
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
