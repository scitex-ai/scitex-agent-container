"""WI-2 — ACL gate on ``message:send`` + spawn-permission gate
(limited scope per lead 2026-05-20).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-2):

  Acceptance (limited scope): an un-permitted (cross-group, no
  grant) sender is rejected with ``403`` + a log line; an
  intra-group sender's message lands; a child's
  ``sac agents start`` is rejected.

The "identity cannot be spoofed via a metadata field" acceptance
criterion was DEFERRED (lead 2026-05-20), later implemented against a
per-node ``node_tokens`` bearer, and REMOVED on 2026-08-28 because
nothing ever minted such a bearer — 0 rows on every fleet host, so the
anti-spoof branch never once fired. The ACL therefore gates on the
self-claimed ``metadata.from_agent`` field, exactly as it did during
the deferral, and every cross-group grant carries that same audit
caveat. The tests below assert what actually gates: the host-wide
bearer at the perimeter, and the name-based ACL behind it.

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
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_blocks import block_send
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    record_comms_policy,
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
        claimed_from_agent=sender,
        target="alice",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_parent_to_child(db_path: Path, pg_schema: str) -> None:
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="root",
        target="worker-a",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_intra_group_sibling_to_sibling(db_path: Path, pg_schema: str) -> None:
    """Handoff §4: 'parent↔child *and* sibling↔sibling, bidirectional'."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="worker-a",
        target="worker-b",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_allows_cross_group_by_default(db_path: Path, pg_schema: str) -> None:
    """Messaging default-allow (operator 2026-07-03): two unrelated
    lineage families, no grant → ALLOW."""
    # Arrange — two unrelated families
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


def test_acl_blocked_sender_is_blocked(db_path: Path, pg_schema: str) -> None:
    """Override preserved: an explicit block still yields a "block"
    decision even under the cross-group default-allow."""
    # Arrange — two unrelated families + an explicit block
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    block_send(sender="child-1", target="child-2")
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert decision == "block"


def test_acl_allows_cross_group_with_explicit_grant(db_path: Path, pg_schema: str) -> None:
    """Explicit cross-group grant flips a deny to allow."""
    # Arrange — two unrelated families + grant child-1 → child-2
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    grant_send(sender="child-1", target="child-2")
    # Act
    decision, _reason = check_send_acl(
        claimed_from_agent="child-1",
        target="child-2",
        db_path=db_path,
    )
    # Assert
    assert decision == "allow"


# ``test_acl_denies_identity_spoof`` and
# ``test_acl_spoof_deny_reason_names_both_identities`` stood here until
# 2026-08-28. Both drove ``check_send_acl(authenticated_node="alice",
# claimed_from_agent="bob")`` — a per-node bearer resolving to one name
# while the metadata claimed another — and asserted the deny. That
# parameter and the branch it fed are gone: nothing in ``src/`` ever
# minted a per-node bearer, so ``authenticated_node`` was ``None`` on
# every real call and the branch these two covered never executed
# outside this file. They are deleted rather than re-pointed because
# there is no longer a second identity to contradict the claim with;
# re-pointing them would have meant asserting a deny the code cannot
# produce. Every assertion below about ``claimed_from_agent`` covers
# LIVE behaviour and is kept.


def test_acl_admin_caller_honors_claimed_from_agent(db_path: Path, pg_schema: str) -> None:
    """Host-wide bearer + ``metadata.from_agent`` set → admin path
    (cross-host forwarder). The metadata claim is honoured verbatim.
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act — the admin caller (host-wide bearer) speaks for root
    decision, _reason = check_send_acl(
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
        claimed_from_agent=sender,
        target="",
        db_path=db_path,
    )
    # Assert
    assert decision == "deny"


# ---------------------------------------------------------------------------
# Spawn-permission gate (check_spawn / spawn_allowed)
# ---------------------------------------------------------------------------


def test_spawn_allows_root_caller(pg_schema: str, db_path: Path) -> None:
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


def test_spawn_denies_child_caller(pg_schema: str, db_path: Path) -> None:
    """A node with a parent (child) is NOT allowed to spawn."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    decision, _reason = check_spawn(caller="worker-a", db_path=db_path)
    # Assert
    assert decision == "deny"


def test_spawn_deny_reason_explains_root_only_policy(pg_schema: str, db_path: Path) -> None:
    """The 403 body names the groups that WOULD authorise the spawn.

    It no longer asserts the caller holds none of them — that claim was
    about the AGENT, and the multi-group defect made it false against
    the same server's own a2a_peers output (2026-08-10).
    """
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    # Act
    _decision, reason = check_spawn(caller="worker-a", db_path=db_path)
    # Assert
    assert reason is not None and "developer, researcher, privileged" in reason


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


def test_http_node_message_send_allows_cross_group_by_default(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """End-to-end: messaging default-allow — a cross-group sender (two
    unrelated lineage families) now lands (< 400)."""
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
    assert r.status_code < 400, r.text


def test_http_node_message_send_403_body_carries_per_spec_reason(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """A per-spec ``inbound.siblings=deny`` override still 403s and the
    body explains the denial (the deny path survives default-allow)."""
    # Arrange — siblings so the per-spec inbound-sibling deny applies.
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    record_comms_policy(name="worker-b", inbound_siblings="deny")
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    body = r.json()
    # Assert
    assert "reason" in body and "inbound deny" in body["reason"]


def test_http_node_message_send_allows_intra_group(
    isolated_listen_env, db_path: Path, pg_schema: str
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
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """A cross-group grant flips the deny to an allow."""
    # Arrange
    record_lineage(child="child-1", parent="root-1", db_path=db_path)
    record_lineage(child="child-2", parent="root-2", db_path=db_path)
    grant_send(sender="child-1", target="child-2")
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
# HTTP-level: the bearer perimeter that actually exists.
#
# Three tests here minted a per-node bearer with ``mint_node_token`` and
# asserted the "identity cannot be spoofed via a metadata field"
# acceptance: worker-a's bearer + ``metadata.from_agent=worker-b`` → 403.
# The feature was removed 2026-08-28 — nothing in ``src/`` ever minted a
# token, so ``node_tokens`` was empty on every host and no request the
# fleet ever served took that path. The three are replaced by the ones
# below, which pin what the perimeter really does with a bearer: the
# host-wide token is admitted, anything else is 403, absence is 401.
# ---------------------------------------------------------------------------


def test_http_host_bearer_with_from_agent_lands(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """The host-wide bearer + ``metadata.from_agent=worker-a`` is the
    only authenticated shape there is, and it lands."""
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


def test_http_unknown_bearer_is_rejected_with_403(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """A bearer that is not the host token is refused. This is the
    verdict a minted per-node token USED to escape; with the table gone
    there is no second bearer that can be admitted."""
    # Arrange
    record_lineage(child="worker-a", parent="root", db_path=db_path)
    record_lineage(child="worker-b", parent="root", db_path=db_path)
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
            headers={"authorization": "Bearer not-the-host-token"},
        )
    # Assert
    assert r.status_code == 403, r.text


def test_http_unknown_bearer_403_body_names_the_bearer_as_the_cause(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """The 403 must say the BEARER was invalid, not that the ACL denied
    — the two are different operator actions."""
    # Arrange
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
            headers={"authorization": "Bearer not-the-host-token"},
        )
    # Assert
    assert r.json().get("error") == "invalid bearer token"


def test_http_missing_bearer_is_rejected_with_401(
    isolated_listen_env, db_path: Path, pg_schema: str
) -> None:
    """No Authorization header at all → 401, distinct from the 403 a
    wrong bearer earns."""
    # Arrange
    app = create_app(token=TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/agents/worker-b/message:send",
            json=_payload("worker-a"),
        )
    # Assert
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# HTTP-level: agents_start denies a child caller with 403.
# ---------------------------------------------------------------------------


def test_http_agents_start_denies_child_caller_with_403(
    pg_schema: str,
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


def test_http_agents_start_403_carries_role_policy_text(
    pg_schema: str,
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
    assert "developer, researcher, privileged" in body_json.get("reason", "")


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
    rows = list_undelivered(target=target)
    return [r for r in rows if (r["event"] or {}).get("kind") == "denied_attempt"]


# Scenarios are computed once per test function via fixtures; each
# downstream test asserts ONE facet of the same arrangement (STX-TQ007:
# one assertion per test). Splitting was the explicit directive for
# this PR's CI green-up; the fixtures keep the heavy arrange/act
# (TestClient + Starlette boot) from re-running per facet.


@pytest.fixture
def cross_group_deny_scenario(isolated_listen_env, db_path: Path, pg_schema: str) -> dict:
    """A denied send via host bearer (admin caller path).

    Since messaging is now DEFAULT-ALLOW cross-group (operator
    2026-07-03), the denied-attempt notification machinery is exercised
    via the surviving deny path: a per-spec ``inbound.siblings=deny`` on
    the target. child-1 and child-2 are siblings under a shared root so
    the sibling relationship applies."""
    record_lineage(child="child-1", parent="root", db_path=db_path)
    record_lineage(child="child-2", parent="root", db_path=db_path)
    record_comms_policy(name="child-2", inbound_siblings="deny")
    app = create_app(token=TOKEN)
    with TestClient(app) as client:
        resp = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1", content="secret body"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    notifs = _denied_attempt_rows(target="child-2", db_path=db_path)
    return {"resp": resp, "notifs": notifs, "db_path": db_path}


def test_cross_group_deny_returns_403_to_sender(cross_group_deny_scenario, pg_schema: str) -> None:
    # Arrange
    resp = cross_group_deny_scenario["resp"]
    # Act
    status = resp.status_code
    # Assert
    assert status == 403, resp.text


def test_cross_group_deny_publishes_one_denied_attempt_to_target_inbox(
    cross_group_deny_scenario, pg_schema: str,
) -> None:
    # Arrange
    notifs = cross_group_deny_scenario["notifs"]
    # Act
    n = len(notifs)
    # Assert
    assert n == 1, notifs


def test_cross_group_deny_notification_identifies_the_sender(
    cross_group_deny_scenario, pg_schema: str,
) -> None:
    # Arrange
    event = cross_group_deny_scenario["notifs"][0]["event"]
    # Act
    sender = event["from_agent"]
    # Assert
    assert sender == "child-1"


def test_cross_group_deny_notification_identifies_the_receiver(
    cross_group_deny_scenario, pg_schema: str,
) -> None:
    # Arrange
    event = cross_group_deny_scenario["notifs"][0]["event"]
    # Act
    receiver = event["to_agent"]
    # Assert
    assert receiver == "child-2"


def test_cross_group_deny_notification_carries_reason(
    cross_group_deny_scenario, pg_schema: str,
) -> None:
    # Arrange
    event = cross_group_deny_scenario["notifs"][0]["event"]
    # Act
    reason = event.get("extra", {}).get("deny_reason", "")
    # Assert
    assert "inbound deny" in reason


def test_cross_group_deny_notification_carries_positive_timestamp(
    cross_group_deny_scenario, pg_schema: str,
) -> None:
    # Arrange
    event = cross_group_deny_scenario["notifs"][0]["event"]
    # Act
    ts = event.get("ts")
    # Assert
    assert isinstance(ts, (int, float)) and ts > 0


# --- Body-leak protection -------------------------------------------------


_SECRET = "PII / credentials / anything the sender shoved in here"


@pytest.fixture
def body_leak_scenario(isolated_listen_env, db_path: Path, pg_schema: str) -> dict:
    """Denied send carrying a secret in its body — must not leak. Denial
    is triggered by a per-spec ``inbound.siblings=deny`` (the surviving
    deny path under messaging default-allow)."""
    record_lineage(child="child-1", parent="root", db_path=db_path)
    record_lineage(child="child-2", parent="root", db_path=db_path)
    record_comms_policy(name="child-2", inbound_siblings="deny")
    app = create_app(token=TOKEN)
    with TestClient(app) as client:
        resp = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1", content=_SECRET),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    notifs = _denied_attempt_rows(target="child-2", db_path=db_path)
    return {"resp": resp, "notifs": notifs}


def test_body_leak_scenario_denies_with_403(body_leak_scenario, pg_schema: str) -> None:
    # Arrange
    resp = body_leak_scenario["resp"]
    # Act
    status = resp.status_code
    # Assert
    assert status == 403, resp.text


def test_body_leak_scenario_notification_content_is_empty(
    body_leak_scenario, pg_schema: str,
) -> None:
    # Arrange
    event = body_leak_scenario["notifs"][0]["event"]
    # Act
    content = event.get("content", "")
    # Assert
    assert content == ""


def test_body_leak_scenario_secret_absent_from_serialized_notification(
    body_leak_scenario, pg_schema: str,
) -> None:
    """Defence-in-depth: the entire stored frame (round-tripped JSON)
    must not contain the secret — guards against a future accidental
    stash on ``extra`` / ``meta`` / ``content``.
    """
    import json as _json

    # Arrange
    event = body_leak_scenario["notifs"][0]["event"]
    # Act
    serialized = _json.dumps(event)
    # Assert
    assert _SECRET not in serialized


# --- Fan-out scoping ------------------------------------------------------


@pytest.fixture
def fanout_scope_scenario(isolated_listen_env, db_path: Path, pg_schema: str) -> dict:
    """A per-spec denied send — only the *real* target's inbox should
    carry a notif; bystander targets and the empty-name inbox stay
    untouched. Denial via ``inbound.siblings=deny`` on child-2 (siblings
    child-1/child-2 under a shared root); bystander is unrelated.
    """
    record_lineage(child="child-1", parent="root", db_path=db_path)
    record_lineage(child="child-2", parent="root", db_path=db_path)
    record_comms_policy(name="child-2", inbound_siblings="deny")
    record_lineage(child="bystander", parent="root-3", db_path=db_path)
    app = create_app(token=TOKEN)
    with TestClient(app) as client:
        resp = client.post(
            "/agents/child-2/message:send",
            json=_payload("child-1"),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    return {
        "resp": resp,
        "target_notifs": _denied_attempt_rows(target="child-2", db_path=db_path),
        "bystander_notifs": _denied_attempt_rows(target="bystander", db_path=db_path),
        "empty_notifs": _denied_attempt_rows(target="", db_path=db_path),
    }


def test_fanout_scope_scenario_target_inbox_has_exactly_one_notif(
    fanout_scope_scenario, pg_schema: str,
) -> None:
    # Arrange
    notifs = fanout_scope_scenario["target_notifs"]
    # Act
    n = len(notifs)
    # Assert
    assert n == 1


def test_fanout_scope_scenario_bystander_inbox_is_empty(
    fanout_scope_scenario, pg_schema: str,
) -> None:
    # Arrange
    notifs = fanout_scope_scenario["bystander_notifs"]
    # Act
    n = len(notifs)
    # Assert
    assert n == 0


def test_fanout_scope_scenario_empty_name_inbox_is_empty(
    fanout_scope_scenario, pg_schema: str,
) -> None:
    # Arrange
    notifs = fanout_scope_scenario["empty_notifs"]
    # Act
    n = len(notifs)
    # Assert
    assert n == 0


# --- Spoof deny records the AUTHENTICATED identity ------------------------
#
# A ``spoof_deny_scenario`` fixture and three tests stood here until
# 2026-08-28. They minted worker-a's per-node bearer, sent with
# ``metadata.from_agent="worker-b"``, and asserted the 403 plus a
# denied-attempt notification naming the AUTHENTICATED identity
# (worker-a) rather than the spoofed claim — the point being that a
# receiver's view of who tried to reach them could not be forged.
#
# The per-node bearer is gone (never minted in ``src/``; 0 rows on every
# fleet host), so ``check_send_acl`` has no second identity to compare
# the claim against and cannot return this deny. Deleted rather than
# re-pointed: the guarantee they asserted was never in force in
# production, where ``authenticated_node`` was always ``None`` and the
# denied-attempt notification therefore already carried the CLAIM. The
# receiver-side notification machinery itself stays fully covered — by
# ``cross_group_deny_scenario`` and ``body_leak_scenario`` above and the
# live-broker fixture below, all of which reach it through the per-spec
# relationship deny, which is a deny the code can actually produce.


# --- Live broker subscriber (fast path) -----------------------------------


@pytest.fixture
def live_broker_event(isolated_listen_env, db_path: Path, pg_schema: str) -> dict:
    """End-to-end on the broker fast path: a live subscriber on the
    target's inbox receives the denied-attempt event the moment the
    denial happens (not just on next reconnect / replay). Uses
    in-process ASGI transport so the subscription and the POST share
    the same event loop — the realistic shape of ``sac mcp channel``
    consuming SSE on the same listen.
    """
    import asyncio

    import httpx

    record_lineage(child="child-1", parent="root", db_path=db_path)
    record_lineage(child="child-2", parent="root", db_path=db_path)
    record_comms_policy(name="child-2", inbound_siblings="deny")
    app = create_app(token=TOKEN)

    async def driver() -> dict:
        broker = app.state.inbox
        q = await broker.subscribe("child-2")
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    "/agents/child-2/message:send",
                    json=_payload("child-1", content="hidden"),
                    headers={"authorization": f"Bearer {TOKEN}"},
                )
                if resp.status_code != 403:
                    raise RuntimeError(
                        f"precondition: expected 403, got "
                        f"{resp.status_code}: {resp.text!r}"
                    )
                event = await asyncio.wait_for(q.get(), timeout=2.0)
                return event
        finally:
            await broker.unsubscribe("child-2", q)

    return asyncio.run(driver())


def test_live_broker_event_kind_is_denied_attempt(live_broker_event, pg_schema: str) -> None:
    # Arrange
    event = live_broker_event
    # Act
    kind = event.get("kind")
    # Assert
    assert kind == "denied_attempt"


def test_live_broker_event_names_the_sender(live_broker_event, pg_schema: str) -> None:
    # Arrange
    event = live_broker_event
    # Act
    sender = event.get("from_agent")
    # Assert
    assert sender == "child-1"


def test_live_broker_event_names_the_receiver(live_broker_event, pg_schema: str) -> None:
    # Arrange
    event = live_broker_event
    # Act
    receiver = event.get("to_agent")
    # Assert
    assert receiver == "child-2"


def test_live_broker_event_content_is_empty(live_broker_event, pg_schema: str) -> None:
    # Arrange
    event = live_broker_event
    # Act
    content = event.get("content", "")
    # Assert
    assert content == ""


def test_live_broker_event_carries_deny_reason(live_broker_event, pg_schema: str) -> None:
    # Arrange
    event = live_broker_event
    # Act
    reason = event.get("extra", {}).get("deny_reason", "")
    # Assert
    assert "inbound deny" in reason


def test_live_broker_event_does_not_leak_body(live_broker_event, pg_schema: str) -> None:
    import json as _json

    # Arrange
    event = live_broker_event
    # Act
    serialized = _json.dumps(event)
    # Assert
    assert "hidden" not in serialized
