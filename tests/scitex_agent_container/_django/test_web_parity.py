"""Web parity increment: fleet quick actions, the a2a panel, the message thread.

Covers the three owner-critical surfaces added to the Django GUI in this
increment, end to end over real HTTP against the ``loopback`` listener. No
mocks, no ``monkeypatch`` (STX-NM002 / PA-306 §3). Each test has AAA markers
and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

import json

from scitex_agent_container._django._constants import (
    CROSSHOST_OPERATORS_ENV,
    IDENTITY_ENV,
    OPERATORS_ENV,
)

OPS_ENV = OPERATORS_ENV
CROSS_ENV = CROSSHOST_OPERATORS_ENV


def _own_row(name="alpha"):
    return {
        "name": name,
        "state_label": "Alive",
        "state_tone": "good",
        "state_detail": "",
        "runtime": "apptainer",
        "billing_mode": "subscription",
        "auth_identity": "anthropic/team-max",
        "runtime_identity_source": "birth_certificate",
        "harness": "anthropic",
        "role": "worker",
        "engine": "anthropic",
        "model": "sonnet",
        "project": "p",
        "host": "this node",
        "scope": "own",
        "cross_host": False,
        "a2a_port": 19000,
        "pid": 1,
        "activity": {
            "operation": {"state": "unknown", "reason": "x"},
            "phase": {"state": "unknown", "reason": "x"},
        },
    }


def _seeded_fleet_html(client, identity, rows) -> str:
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put(identity, rows)
    try:
        return client.get("/").content.decode()
    finally:
        CACHE.clear()


# ── fleet per-row quick actions ─────────────────────────────────────────────
def test_fleet_quick_actions_visible_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = _seeded_fleet_html(client, "op1", [_own_row()])
    # Assert
    assert 'value="stop"' in html and "/alpha/message" in html


def test_fleet_quick_actions_hidden_from_non_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = _seeded_fleet_html(client, "alice", [_own_row()])
    # Assert
    assert 'value="stop"' not in html and "/alpha/message" not in html


def test_fleet_quick_actions_hidden_on_cross_host_for_local_only(
    client, loopback, env_save_restore
):
    # Arrange — op1 is a local operator but NOT in the cross-host list; the
    # row is tagged cross-host, so the row gate must stay shut.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "someone-else")
    row = _own_row("gamma")
    row.update({"scope": "cross-host", "cross_host": True})
    # Act
    html = _seeded_fleet_html(client, "op1", [row])
    # Assert
    assert 'value="stop"' not in html and "/gamma/message" not in html


def test_fleet_quick_actions_shown_on_cross_host_for_operator(
    client, loopback, env_save_restore
):
    # Arrange — op1 is in BOTH lists, so the cross-host row gate opens.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    row = _own_row("gamma")
    row.update({"scope": "cross-host", "cross_host": True})
    # Act
    html = _seeded_fleet_html(client, "op1", [row])
    # Assert
    assert 'value="stop"' in html and "/gamma/message" in html


def test_fleet_row_message_post_reports_delivery(client, loopback, env_save_restore, audit_log):
    # Arrange — the quick-action message form POSTs to the existing route.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act — the turn bridge is unreachable in tests, so this is a failed
    # delivery reported back, never a silent drop and never a 500.
    resp = client.post("/alpha/message", {"message": "steer left"})
    # Assert
    assert resp.status_code in (302, 303) and "operation=message" in resp["Location"]


# ── message/steer thread on detail ──────────────────────────────────────────
def test_detail_notice_banner_shows_delivery(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act — the banner the message/lifecycle POST-redirects land on.
    html = client.get("/alpha/?operation=message&state=delivered&message=hello").content.decode()
    # Assert
    assert "message: delivered" in html and "hello" in html


def test_detail_notice_rejects_arbitrary_operation(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act — a query string the server never minted must not render a banner.
    html = client.get("/alpha/?operation=evil&state=delivered&message=x").content.decode()
    # Assert
    assert "agents-bannor" not in html and "evil" not in html


def test_detail_thread_card_shows_session_history(client, loopback, env_save_restore):
    # Arrange — the daemon's stored history is the session log tail.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "Message thread" in html and "start the job" in html


def test_detail_thread_card_without_log_invites_first_message(
    client, loopback, env_save_restore
):
    # Arrange — beta has no session log on the loopback listener.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/beta/").content.decode()
    # Assert
    assert "Message thread" in html and "Send the first one" in html


# ── a2a panel ───────────────────────────────────────────────────────────────
def test_a2a_panel_lists_peer_ports(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/a2a/").content.decode()
    # Assert
    assert "Peer reachability" in html and "19000" in html


def test_a2a_panel_allowlist_unavailable_without_store(client, loopback, env_save_restore):
    # Arrange — no grant-store route from this host (no PG cluster here), so
    # the panel must say so explicitly instead of an empty "no grants" table.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/a2a/").content.decode()
    # Assert
    assert "not available from this host" in html


def test_a2a_mutation_forms_hidden_from_non_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/a2a/").content.decode()
    # Assert
    assert 'name="decision"' not in html and "Read-only" in html


def test_a2a_mutation_forms_shown_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = client.get("/a2a/").content.decode()
    # Assert
    assert 'value="revoke"' in html and 'value="grant"' in html


def test_a2a_action_denied_for_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    resp = client.post("/a2a/action", {"decision": "grant", "sender": "alpha", "target": "beta"})
    # Assert
    assert resp.status_code == 403


def test_a2a_action_unknown_decision_is_400(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    resp = client.post("/a2a/action", {"decision": "ban", "sender": "alpha", "target": "beta"})
    # Assert
    assert resp.status_code == 400


def test_a2a_action_allowed_redirects_and_audits(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act — the grant store is unreachable here, so the delegate fails and
    # the view must still redirect + audit (state=failed), never 500.
    resp = client.post("/a2a/action", {"decision": "grant", "sender": "alpha", "target": "beta"})
    rec = json.loads(audit_log.read_text().strip().splitlines()[-1])
    # Assert
    assert resp.status_code in (302, 303) and rec["event"] == "a2a_acl_action"


def test_mounted_a2a_uses_hub_shell(hub_client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = hub_client.get("/apps/agents/a2a/").content.decode()
    # Assert
    assert 'id="hub-global-header"' in html and "workspace-three-col" not in html
