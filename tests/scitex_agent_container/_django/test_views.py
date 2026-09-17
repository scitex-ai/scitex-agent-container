"""Tests for ``_django/views`` — the browser slice, end to end over real HTTP.

The view is driven through Django's test client against a REAL loopback listener
(the ``loopback`` fixture serves the contract); the app reaches it through
``RemoteFleet.from_environment()`` with no patching. Env (identity, allowlists,
audit path) is set via the shared function-scoped ``env_save_restore``. Each
test has AAA markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container import __path__ as _package_paths
from scitex_agent_container._django._constants import (
    CROSSHOST_OPERATORS_ENV,
    IDENTITY_ENV,
    OPERATORS_ENV,
)

OPS_ENV = OPERATORS_ENV
CROSS_ENV = CROSSHOST_OPERATORS_ENV


# ── fleet scoping ─────────────────────────────────────────────────────────────
def test_fleet_hides_cross_host_from_ordinary(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "gamma" not in html and "alpha" in html


def test_fleet_crosshost_operator_sees_all(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "gamma" in html and "cross-host" in html


def test_fleet_api_scopes_to_identity(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    data = json.loads(client.get("/api/fleet").content)
    # Assert
    assert {a["name"] for a in data["agents"]} == {"alpha", "beta", "delta"}


def test_fleet_api_reports_unreachable_listener(client, unreachable_listener):
    # Arrange
    client = client  # uses the closed-port / no-token env set by the fixture
    # Act
    resp = client.get("/api/fleet")
    # Assert
    assert resp.status_code == 502 and json.loads(resp.content)["ok"] is False


def test_fleet_page_shows_error_state_when_unreachable(client, unreachable_listener):
    # Arrange
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "could not reach the SAC host control plane" in html


# ── lifecycle control + audit ─────────────────────────────────────────────────
def test_lifecycle_denied_for_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    resp = client.post("/alpha/action", {"action": "stop"})
    # Assert
    assert resp.status_code == 403


def test_lifecycle_own_agent_allowed_and_audited(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    resp = client.post("/alpha/action", {"action": "stop"})
    rec = json.loads(audit_log.read_text().strip().splitlines()[-1])
    # Assert
    assert resp.status_code in (302, 303) and rec["event"] == "lifecycle_action"


def test_lifecycle_unsupported_action_is_400(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    resp = client.post("/alpha/action", {"action": "reboot"})
    # Assert
    assert resp.status_code == 400


def test_lifecycle_cross_host_denied_for_local_only(client, loopback, env_save_restore, audit_log):
    # Arrange — op1 is a local operator but NOT in the cross-host list.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "someone-else")
    # Act
    resp = client.post("/gamma/action", {"action": "restart"})
    # Assert
    assert resp.status_code == 403


def test_lifecycle_cross_host_allowed_and_audited(client, loopback, env_save_restore, audit_log):
    # Arrange — op1 is in BOTH lists.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    # Act
    resp = client.post("/gamma/action", {"action": "restart"})
    recs = [json.loads(line) for line in audit_log.read_text().strip().splitlines()]
    granted = [r for r in recs if r["event"] == "lifecycle_action" and r["agent"] == "gamma"]
    # Assert
    assert resp.status_code in (302, 303) and granted and granted[-1]["cross_host"] is True


# ── detail ────────────────────────────────────────────────────────────────────
def test_detail_own_agent_shows_state(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "Alive" in html and "alpha" in html


def test_detail_names_unpublished_activity_as_unknown(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/beta/").content.decode()
    # Assert
    assert "Runtime activity" in html and html.count(">Unknown</span>") >= 1


def test_fleet_shows_published_operation_and_phase(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "busy" in html and "reviewing" in html


def test_fleet_shows_harness_engine_and_model(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")

    # Act
    html = client.get("/").content.decode()

    # Assert
    assert "Harness" in html and "Engine / Model" in html and "anthropic" in html and "sonnet" in html


def test_mobile_fleet_keeps_harness_and_engine_columns_visible():
    # Arrange
    css_path = (
        Path(next(iter(_package_paths)))
        / "_django"
        / "static"
        / "scitex_agent_container"
        / "agents.css"
    )

    # Act
    css = css_path.read_text(encoding="utf-8")

    # Assert
    assert "nth-child(4) { display: none; }" not in css and "nth-child(5) { display: none; }" not in css


def test_detail_cross_agent_hidden_from_ordinary(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/gamma/").content.decode()
    # Assert
    assert "not in scope" in html


def test_detail_cross_agent_visible_to_crosshost_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    # Act
    html = client.get("/gamma/").content.decode()
    # Assert
    assert "Resources" in html and "not in scope" not in html


def test_detail_shows_redacted_session_tail(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "start the job" in html and "sk-abc123DEF456GHI789jkl012" not in html


def test_detail_without_log_says_no_log(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/beta/").content.decode()
    # Assert
    assert "No session log recorded yet." in html


def test_detail_resources_honest_namespace_state(client, loopback, env_save_restore):
    # Arrange — alpha's pid (111) is not visible in this namespace.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "separate pid namespace" in html


def test_detail_spec_invalid_agent_shows_typed_state(client, loopback, env_save_restore):
    # Arrange — delta's /status returns a typed 400.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/delta/").content.decode()
    # Assert
    assert "Spec invalid" in html


def test_detail_operate_gate_for_non_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "Read-only" in html


def test_detail_operate_gate_for_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "Start</button>" in html


# ── polish: accessible action column + real tokens ───────────────────────────
def test_fleet_labels_action_column(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert '<th class="row-actions">Actions</th>' in html


# ── dual-mode: mounted in the Hub shell (global_base) not the standalone shell ─
def test_mounted_uses_hub_shell(hub_client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = hub_client.get("/apps/agents/").content.decode()
    # Assert
    assert 'id="hub-global-header"' in html and "workspace-three-col" not in html


def test_mounted_links_prefix_aware(hub_client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = hub_client.get("/apps/agents/").content.decode()
    # Assert
    assert 'href="/apps/agents/alpha/"' in html


def test_mounted_detail_uses_hub_shell(hub_client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = hub_client.get("/apps/agents/alpha/").content.decode()
    # Assert
    assert 'id="hub-global-header"' in html and "workspace-three-col" not in html


def test_standalone_uses_standalone_shell(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/").content.decode()
    # Assert
    assert "workspace-three-col" in html


# ── polish: real scitex-ui tokens + action-column styling (file-based) ─────────
def test_css_uses_real_scitex_ui_tokens():
    # Arrange
    css = (Path(__file__).resolve().parents[3] / "src" / "scitex_agent_container" / "_django" / "static" / "scitex_agent_container" / "agents.css").read_text(encoding="utf-8")
    # Act
    uses_tokens = "var(--text-secondary" in css and "var(--accent" in css
    # Assert
    assert uses_tokens and "--stx-text" not in css


def test_css_action_header_right_aligned():
    # Arrange
    css = (Path(__file__).resolve().parents[3] / "src" / "scitex_agent_container" / "_django" / "static" / "scitex_agent_container" / "agents.css").read_text(encoding="utf-8")
    # Act
    aligned = ".row-actions { text-align: right" in css.replace("  ", " ")
    # Assert
    assert aligned
