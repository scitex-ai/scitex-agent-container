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

from django.template.loader import render_to_string

from scitex_agent_container import __path__ as _package_paths
from scitex_agent_container._django._constants import (
    CROSSHOST_OPERATORS_ENV,
    IDENTITY_ENV,
    OPERATORS_ENV,
)

OPS_ENV = OPERATORS_ENV
CROSS_ENV = CROSSHOST_OPERATORS_ENV


# ── fleet scoping ─────────────────────────────────────────────────────────────
def test_fleet_inventory_hides_cross_host_from_ordinary(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act: the INVENTORY read, not the shell first paint (P0: the shell renders
    # before any read completes, so it is legitimately empty on a cold cache).
    data = json.loads(client.get("/api/fleet").content)
    names = {a["name"] for a in data["agents"]}
    # Assert: the scope rule still holds on the data path.
    assert "gamma" not in names and "alpha" in names


def test_fleet_inventory_crosshost_operator_sees_all(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    # Act
    data = json.loads(client.get("/api/fleet").content)
    names = {a["name"] for a in data["agents"]}
    # Assert: an authorized operator's read still includes the cross-host row.
    assert "gamma" in names


def test_fleet_api_scopes_to_identity(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    data = json.loads(client.get("/api/fleet").content)
    # Assert
    assert {a["name"] for a in data["agents"]} == {"alpha", "beta", "delta"}


def test_fleet_uses_one_batched_agents_request_and_zero_status_fanout(
    client, listener_requests, env_save_restore
):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")

    # Act
    response = client.get("/api/fleet")

    # Assert
    assert response.status_code == 200 and listener_requests == ["/agents"]


def test_fleet_api_reports_unreachable_listener(client, unreachable_listener):
    # Arrange
    client = client  # uses the closed-port / no-token env set by the fixture
    # Act
    resp = client.get("/api/fleet")
    # Assert
    assert resp.status_code == 502 and json.loads(resp.content)["ok"] is False


def test_fleet_page_shows_error_state_when_unreachable(client, unreachable_listener):
    # Arrange
    # Act: a cold cache with a dead listener renders the SHELL immediately.
    html = client.get("/").content.decode()
    # Assert: a real user-facing state, with no internal detail (P0).
    assert 'data-fleet-state="loading"' in html or 'data-fleet-state="unavailable"' in html


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


def test_fleet_inventory_shows_published_operation_and_phase(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act: the INVENTORY read (P0: the shell first paint renders before any read
    # completes, so activity values are not on it).
    data = json.loads(client.get("/api/fleet").content)
    activities = json.dumps([a.get("activity", {}) for a in data["agents"]])
    # Assert: runner-published activity is still projected through.
    assert "busy" in activities and "reviewing" in activities


def _render_fleet_inventory(client) -> str:
    response = client.get("/api/fleet")
    assert response.status_code == 200
    data = json.loads(response.content)
    assert data["ok"] is True and data["agents"]
    return render_to_string(
        "scitex_agent_container/_fleet_content.html",
        {
            "agents": data["agents"],
            "summary": data["summary"],
            "identity": data["identity"],
            "crosshost_authorized": False,
            "comm_error": "",
            "fleet_state": "ok",
            "url_base": "",
        },
    )


def test_fleet_shows_billing_auth_harness_engine_and_model(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")

    # Act
    html = _render_fleet_inventory(client)

    # Assert
    assert (
        "Billing" in html
        and "Auth identity" in html
        and "Harness" in html
        and "Engine / Model" in html
        and "subscription" in html
        and "anthropic/team-max" in html
        and "sonnet" in html
    )


def test_compact_fleet_visibly_qualifies_runtime_identity_source(
    client, loopback, env_save_restore
):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")

    # Act
    html = _render_fleet_inventory(client)

    # Assert
    assert '<span class="identity-source">birth_certificate</span>' in html


def test_fleet_template_escapes_all_runtime_identity_cells() -> None:
    # Arrange
    hostile = '<script>alert("x")</script>'
    agent = {
        "name": hostile,
        "state_tone": "good",
        "state_detail": hostile,
        "state_label": hostile,
        "cross_host": False,
        "runtime": hostile,
        "billing_mode": hostile,
        "auth_identity": hostile,
        "runtime_identity_source": hostile,
        "harness": hostile,
        "engine": hostile,
        "model": hostile,
        "host": hostile,
        "a2a_port": None,
        "activity": {
            "operation": {"state": "unknown", "reason": hostile},
            "phase": {"state": "unknown", "reason": hostile},
        },
    }

    # Act
    html = render_to_string(
        "scitex_agent_container/_fleet_content.html",
        {
            "agents": [agent],
            "summary": {"total": 1, "alive": 1, "attention": 0, "cross_host": 0},
            "listener": hostile,
            "identity": hostile,
            "url_base": "",
            "comm_error": "",
            "crosshost_authorized": False,
        },
    )

    # Assert
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_mobile_fleet_keeps_identity_and_runtime_columns_visible():
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
    assert all(
        f"nth-child({column}) {{ display: none; }}" not in css
        for column in (4, 5, 6, 7)
    )


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
    # Arrange: seed a last-known snapshot so the table renders. P0 made the
    # first paint deliberately empty on a cold cache, so a populated render is
    # now something a test must set up rather than assume.
    from scitex_agent_container._django._inventory_cache import CACHE

    env_save_restore.set(IDENTITY_ENV, "alice")
    CACHE.put("alice", [{"name": "alpha", "state_label": "Alive", "state_tone": "good",
                         "runtime": "apptainer", "harness": "anthropic", "role": "worker",
                         "engine": "anthropic", "model": "sonnet", "project": "p",
                         "host": "this node", "scope": "own", "cross_host": False,
                         "a2a_port": 19000, "pid": 1, "activity": {}}])
    # Act
    html = client.get("/").content.decode()
    # Assert: the action column keeps its accessible label.
    assert '<th class="row-actions">Actions</th>' in html
    CACHE.clear()


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
    # Warm the last-known snapshot via a live read: the shell renders the table
    # from cache on the next paint; a cold first paint is legitimately `loading`
    # (B6: this test must not rely on another test's leaked cache state).
    hub_client.get("/apps/agents/api/fleet")
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


# ── B1: cold-shell transition + B3 scope + B2 redaction (six-blocker regressions) ──
# Tracked RED-first regressions. They use only the loopback / unreachable
# listeners and the public HTTP surface, so they run identically against the
# baseline archive (RED) and the fix (GREEN).
FORBIDDEN_DETAIL = ("127.0.0.1:1", "could not reach", "Errno", "urlopen", "Connection")


def test_cold_shell_loading_does_not_claim_empty(client, unreachable_listener, env_save_restore):
    # Arrange: cold cache, unreachable control plane.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act: the first paint of the shell.
    html = client.get("/").content.decode()
    # Assert: loading is its own state and must not also say "no agents".
    assert 'data-fleet-state="loading"' in html and "No agents visible" not in html


def test_cold_shell_renders_a_readonly_inventory_poll(client, unreachable_listener, env_save_restore):
    # Arrange: cold cache, unreachable control plane.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act: the shell first paint carries the client-side transition.
    html = client.get("/").content.decode()
    # Assert: it polls the inventory endpoint (a GET) to leave the loading state.
    assert "fetch" in html and "/api/fleet" in html and "window.location.reload" in html


def test_completed_failure_transitions_to_unavailable_with_retry(client, unreachable_listener, env_save_restore):
    # Arrange: a COMPLETED read failure is the last thing observed for this
    # identity. Store it as an error snapshot; the background refresh also fails
    # here, so on the fix the error is recorded (unavailable + Retry) instead of
    # being dropped (perpetual loading).
    from scitex_agent_container._django import _inventory_cache as _m

    _m.CACHE.put("alice", [], error="the control plane did not answer")
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act: render the shell for the identity whose last observation was a failure.
    html = client.get("/").content.decode()
    # Assert: it shows unavailable with a Retry control, not a perpetual spinner.
    assert 'data-fleet-state="unavailable"' in html and "Retry" in html


def test_fleet_api_error_is_redacted(client, unreachable_listener, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    body = client.get("/api/fleet").content.decode()
    # Assert: the operator-facing error carries no internal listener detail.
    assert not any(token in body for token in FORBIDDEN_DETAIL)


def test_detail_reveals_no_listener_detail_when_unreachable(client, unreachable_listener, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert
    assert "127.0.0.1:1" not in html and "could not reach" not in html


def test_lifecycle_reveals_no_listener_detail_when_unreachable(client, unreachable_listener, env_save_restore, audit_log):
    # Arrange: an authorized operator acts while the control plane is down.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act: the POST returns a JSON 502 (list_all fails before any redirect).
    resp = client.post("/alpha/action", {"action": "stop"})
    # Assert: the JSON error is redacted (no internal detail reaches the browser).
    assert resp.status_code == 502 and not any(
        token in resp.content.decode() for token in FORBIDDEN_DETAIL
    )


def test_revoked_crosshost_scope_does_not_serve_a_cached_crosshost_row(client, loopback, env_save_restore):
    # Arrange: while granted cross-host, a live read seeds the last-known
    # snapshot for this identity (the view stores projected, scoped rows).
    from scitex_agent_container._django import _inventory_cache as _m

    env_save_restore.set(IDENTITY_ENV, "op")
    env_save_restore.set(CROSS_ENV, "op")
    granted = json.loads(client.get("/api/fleet").content)
    _m.CACHE.put("op", granted["agents"])  # model the view's last-known snapshot
    # Act: the cross-host grant is revoked (config change); the same identity
    # renders the shell from its last-known snapshot.
    env_save_restore.delete(CROSS_ENV)
    html = client.get("/").content.decode()
    # Assert: the revoked cross-host row is NOT served from the granted-scope cache.
    assert "gamma" not in html


def test_typed_remote_error_raw_detail_does_not_reach_state_detail_or_template():
    # Arrange: a TYPED listener error whose human message embeds internal
    # deployment detail (the listener endpoint plus a transport reason). The
    # typed label is operator-facing; the transport detail is not.
    from scitex_agent_container._django._projection import project_row
    from scitex_agent_container._django._remote import RemoteOperationError

    exc = RemoteOperationError(
        502,
        "could not reach http://127.0.0.1:1 (urlopen error [Errno 111] Connection refused)",
        kind="spec_resolution_failed",
    )
    # Act: the row the shell projects, and the template output that renders it.
    projected = project_row({"name": "delta"}, exc)
    html = render_to_string(
        "scitex_agent_container/_fleet_content.html",
        {
            "agents": [projected],
            "summary": {"total": 1, "alive": 0, "attention": 1, "cross_host": 0},
            "listener": "",
            "identity": "alice",
            "url_base": "",
            "comm_error": "",
            "crosshost_authorized": False,
        },
    )
    # Assert: the typed label survives; the raw deployment detail reaches neither
    # state_detail nor the rendered template.
    assert projected["state_label"] == "Spec invalid" and not any(
        token in (projected["state_detail"] + html) for token in FORBIDDEN_DETAIL
    )


def test_typed_detail_redaction_publishes_no_raw_shape_at_all():
    # Arrange: the deployment/credential shapes a typed listener message can
    # embed - a Bearer marker, a bare host:port, a bracketed IPv6 literal.
    from scitex_agent_container._django._remote import redact_detail

    hostile = (
        "Bearer SYNTHETIC_CREDENTIAL_MARKER",
        "listener scitex-compute-fixture:7878",
        "listener [fd00::123]:7878",
    )
    # Act
    published = [redact_detail(text) for text in hostile]
    # Assert: raw text is NEVER published - least disclosure, so every one of
    # these maps to the one fixed phrase rather than being masked in place.
    assert all(
        out != src and "Bearer" not in out and ":" not in out and "[" not in out
        for src, out in zip(hostile, published)
    ) and len(set(published)) == 1


def test_typed_detail_does_not_publish_an_alphabetic_internal_hostname():
    # Arrange: a typed error whose message embeds an internal host. The message
    # is wholly alphabetic prose, so any grammar-shaped gate publishes it
    # verbatim - which is exactly how a hostname reaches the browser.
    from scitex_agent_container._django._projection import project_row
    from scitex_agent_container._django._remote import RemoteOperationError

    exc = RemoteOperationError(400, "listener compute-fixture.internal", kind="spec_unreadable")
    # Act
    detail = project_row({"name": "delta"}, exc)["state_detail"]
    # Assert: the browser gets the FIXED public text for the typed code, never
    # the message, so the host cannot survive whatever its shape.
    assert detail == "The agent's spec could not be read." and "internal" not in detail


def test_typed_detail_does_not_publish_an_alphabetic_credential_phrase():
    # Arrange: a credential-phrased message that is also wholly alphabetic, so
    # no punctuation or secret-word rule would catch it either.
    from scitex_agent_container._django._projection import project_row
    from scitex_agent_container._django._remote import RemoteOperationError

    exc = RemoteOperationError(400, "api key SYNTHETICONLYVALUE", kind="spec_resolution_failed")
    # Act
    detail = project_row({"name": "delta"}, exc)["state_detail"]
    # Assert: the message is not published at all, phrased however it is.
    assert (
        detail == "The agent's spec could not be validated."
        and "SYNTHETICONLYVALUE" not in detail
    )


def test_typed_detail_does_not_publish_the_prior_internal_or_local_hostname_shape():
    # Arrange: the shape the earlier denylist masked (`*.internal` / `*.local`).
    # Naming an internal host must not depend on how the host is punctuated.
    from scitex_agent_container._django._projection import project_row
    from scitex_agent_container._django._remote import RemoteOperationError

    exc = RemoteOperationError(409, "listener compute-fixture.local", kind="ambiguous_registry")
    # Act
    detail = project_row({"name": "delta"}, exc)["state_detail"]
    # Assert: least disclosure - the fixed text for the code, nothing of the host.
    assert (
        detail == "More than one registry claims this agent's name."
        and "compute-fixture" not in detail
    )
