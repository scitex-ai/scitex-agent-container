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
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    mint_node_token,
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
        authenticated_node=sender,
        claimed_from_agent=sender,
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_parent_to_child(db_path: Path) -> None:
    # Arrange
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
    # Arrange — two unrelated families
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
    # Arrange
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


def test_acl_allows_cross_group_with_explicit_grant(db_path: Path) -> None:
    """Explicit cross-group grant flips a deny to allow."""
    # Arrange — two unrelated families + grant child-1 → child-2
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    grant_send(sender="child-1", target="child-2", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        authenticated_node="child-1",
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_denies_identity_spoof(db_path: Path) -> None:
    """Handoff §4 acceptance: "identity cannot be spoofed via a
    metadata field". A per-node bearer authenticates one name; if
    ``metadata.from_agent`` claims a different name → 403.
    """
    # Arrange
    record_lineage(child="alice", parent="root", db_path=db_path)
    record_lineage(child="bob", parent="root", db_path=db_path)
    # Act — alice's bearer, bob's claim
    decision, _reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="bob",
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_acl_spoof_deny_reason_names_both_identities(db_path: Path) -> None:
    """The 403 body explains *which* identity claimed to be whom."""
    # Arrange
    # Act
    _decision, reason = check_send_acl(
        authenticated_node="alice",
        claimed_from_agent="bob",
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert reason is not None and "alice" in reason and "bob" in reason


def test_acl_admin_caller_honors_claimed_from_agent(db_path: Path) -> None:
    """Host-wide bearer + ``metadata.from_agent`` set → admin path
    (cross-host forwarder). The metadata claim is honoured verbatim.
    """
    # Arrange
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


def test_acl_denies_when_no_identity_at_all(db_path: Path) -> None:
    """Host bearer + missing ``metadata.from_agent`` → no identity to
    gate on → 403.
    """
    # Arrange
    # Act
    decision, _reason = check_send_acl(
        authenticated_node=None,
        claimed_from_agent=None,
        target="anyone",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


def test_acl_denies_when_target_missing(db_path: Path) -> None:
    # Arrange
    sender = "alice"
    # Act
    decision, _reason = check_send_acl(
        authenticated_node=sender,
        claimed_from_agent=sender,
        target="",
        db_path=db_path,
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
# HTTP-level: per-node bearer enforces "identity cannot be spoofed via a
# metadata field" (handoff §4 acceptance).
# ---------------------------------------------------------------------------


def test_http_per_node_bearer_allows_matching_from_agent(
    isolated_listen_env, db_path: Path
) -> None:
    """Per-node bearer for worker-a + ``metadata.from_agent=worker-a``
    + intra-group target → allow.
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    worker_a_token = mint_node_token(name="worker-a", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
            headers={"authorization": f"Bearer {worker_a_token}"},
        )
    # Assert
    assert r.status_code < 400, r.text


def test_http_per_node_bearer_denies_spoofed_from_agent_with_403(
    isolated_listen_env, db_path: Path
) -> None:
    """Per-node bearer for worker-a + ``metadata.from_agent=worker-b``
    → 403 identity spoof (the acceptance criterion).
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    worker_a_token = mint_node_token(name="worker-a", db_path=db_path)
    mint_node_token(name="worker-b", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act — worker-a's bearer, but claim to be worker-b
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-b"),
            headers={"authorization": f"Bearer {worker_a_token}"},
        )
    # Assert
    assert r.status_code == 403, r.text


def test_http_per_node_bearer_403_body_explains_spoof(
    isolated_listen_env, db_path: Path
) -> None:
    """The 403 body identifies the resolved name vs the claimed
    name so the operator can see which identity tried to spoof."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    worker_a_token = mint_node_token(name="worker-a", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-b"),
            headers={"authorization": f"Bearer {worker_a_token}"},
        )
    body = r.json()
    # Assert
    reason = body.get("reason", "")
    assert "spoof" in reason and "worker-a" in reason and "worker-b" in reason


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


# ---------------------------------------------------------------------------
# Comms item D — denied-attempt notification reaches the RECEIVER.
#
# Without this, the 403 only travels to the sender; the receiver has no
# visibility into "X tried to reach me and was denied" and cannot decide
# whether to grant. Per the lead's comms-D directive: on an ACL-denied
# send the receiver MUST learn about the attempt via the same broker /
# inbox channel they subscribe to — but the message body must NEVER
# leak (only attempt metadata: from, to, reason, timestamp).
# ---------------------------------------------------------------------------


def _denied_attempt_rows(target: str, db_path: Path) -> list[dict]:
    """Return every persisted ``kind="denied_attempt"`` row for ``target``.

    Uses :func:`list_undelivered` — the same query the SSE inbox stream
    runs on a fresh subscriber, so what this returns is exactly what a
    receiver coming online sees after the denial.
    """
    rows = list_undelivered(target=target, db_path=db_path)
    return [
        r for r in rows if (r["event"] or {}).get("kind") == "denied_attempt"
    ]


def test_acl_deny_publishes_denied_attempt_notification_to_target(
    isolated_listen_env, db_path: Path
) -> None:
    """Comms item D: a cross-group denied send (a) 403s the sender AND
    (b) leaves a ``kind="denied_attempt"`` event on the target's inbox
    channel for the receiver to consume on connect.
    """
    # Arrange — two unrelated families, no grant.
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1", content="secret body"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert — (a) 403 to the sender (existing contract preserved).
    assert r.status_code == 403, r.text
    # Assert — (b) the receiver's inbox channel has the denied-attempt notif.
    notifs = _denied_attempt_rows(target="child-2", db_path=db_path)
    assert len(notifs) == 1, notifs
    event = notifs[0]["event"]
    # The notification identifies WHO tried and WHO they tried to reach.
    assert event["from_agent"] == "child-1"
    assert event["to_agent"] == "child-2"
    # And carries the same reason the sender saw on its 403.
    assert (
        event.get("extra", {}).get("deny_reason")
        and "cross-group" in event["extra"]["deny_reason"]
    )
    # And a timestamp the receiver can render.
    assert isinstance(event.get("ts"), (int, float)) and event["ts"] > 0


def test_acl_deny_notification_does_not_leak_message_body(
    isolated_listen_env, db_path: Path
) -> None:
    """The body must never leak to an unauthorized receiver — only
    attempt metadata. The persisted notif's ``content`` is the empty
    string regardless of what the denied sender tried to send.
    """
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    app = create_app(token=TOKEN)
    secret = "PII / credentials / anything the sender shoved in here"
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1", content=secret),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert — 403 returned, and the receiver's inbox row has no body.
    assert r.status_code == 403, r.text
    notifs = _denied_attempt_rows(target="child-2", db_path=db_path)
    assert len(notifs) == 1
    event = notifs[0]["event"]
    assert event.get("content", "") == ""
    # And the raw stored frame (meta_json round-trip) contains nothing
    # that looks like the secret — defence-in-depth against a future
    # accidental stash on ``extra`` / ``meta``.
    import json as _json

    assert secret not in _json.dumps(event)


def test_acl_deny_notification_skipped_when_target_missing(
    isolated_listen_env, db_path: Path
) -> None:
    """A ``missing target`` deny has no inbox to notify — must not
    crash and must not persist a stray notification under "".
    """
    # Arrange — empty target denied at the ACL layer is unreachable via
    # the route (path requires <name>), but unit-level the deny path
    # is the same; here we exercise the cross-group HTTP deny with a
    # well-formed target and confirm an UNRELATED target's inbox stays
    # empty (regression guard against a fan-out bug).
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    record_lineage(child="bystander", parent="root-3", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    # Assert
    assert r.status_code == 403
    # Only the actual target's inbox carries the notif; bystanders are
    # untouched.
    assert len(_denied_attempt_rows(target="child-2", db_path=db_path)) == 1
    assert _denied_attempt_rows(target="bystander", db_path=db_path) == []
    assert _denied_attempt_rows(target="", db_path=db_path) == []


def test_acl_deny_notification_records_spoofed_identity_resolution(
    isolated_listen_env, db_path: Path
) -> None:
    """On an identity-spoof deny (per-node bearer for X, claims to be Y),
    the receiver's notification names the AUTHENTICATED identity, not
    the spoofed claim — otherwise an attacker could forge the receiver's
    view of who attempted to reach them.
    """
    # Arrange — worker-a and worker-b are siblings (intra-group); the
    # spoof attempt comes from worker-a's bearer claiming to be worker-b
    # while targeting worker-b. ACL denies as spoof regardless of the
    # group relation.
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    worker_a_token = mint_node_token(name="worker-a", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act — worker-a's bearer, claim to be worker-b, target worker-b.
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-b"),
            headers={"authorization": f"Bearer {worker_a_token}"},
        )
    # Assert
    assert r.status_code == 403
    notifs = _denied_attempt_rows(target="worker-b", db_path=db_path)
    assert len(notifs) == 1
    event = notifs[0]["event"]
    # The receiver sees the AUTHENTICATED identity (worker-a), not the
    # spoofed claim (worker-b).
    assert event["from_agent"] == "worker-a"
    assert "spoof" in event.get("extra", {}).get("deny_reason", "")


def test_acl_deny_publishes_to_live_broker_subscriber(
    isolated_listen_env, db_path: Path
) -> None:
    """End-to-end on the broker fast path: a live subscriber on the
    target's inbox channel receives the denied-attempt event the moment
    the denial happens (not just on next reconnect / replay).

    Uses an in-process ASGI transport so the broker subscription and
    the POST share the same event loop — that's the realistic shape
    of ``sac mcp channel`` consuming SSE from the same listen.
    """
    import asyncio

    import httpx

    # Arrange — two unrelated families, no grant.
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    app = create_app(token=TOKEN)

    async def driver() -> dict:
        # Subscribe to the broker on the same loop the ASGI app will
        # publish on. Then POST the denied send and pull the event.
        broker = app.state.inbox
        q = await broker.subscribe("child-2")
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r = await client.post(
                    "/agents/child-2/message:send",
                    json=_payload("child-1", content="hidden"),
                    headers={"authorization": f"Bearer {TOKEN}"},
                )
                assert r.status_code == 403, r.text
                # Live subscriber MUST get the denied-attempt event.
                event = await asyncio.wait_for(q.get(), timeout=2.0)
                return event
        finally:
            await broker.unsubscribe("child-2", q)

    event = asyncio.run(driver())
    # Assert — shape matches the persisted-row contract.
    assert event.get("kind") == "denied_attempt"
    assert event.get("from_agent") == "child-1"
    assert event.get("to_agent") == "child-2"
    assert event.get("content", "") == ""
    assert "cross-group" in event.get("extra", {}).get("deny_reason", "")
    # No body leak even on the live publish path.
    assert "hidden" not in __import__("json").dumps(event)
