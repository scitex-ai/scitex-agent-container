"""View tests: the browser slice (scoping, control gating, audit) end to end."""

from __future__ import annotations

import json


def test_fleet_page_ordinary_identity_hides_cross_host(client, fake_fleet, monkeypatch):
    monkeypatch.delenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", raising=False)
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "alpha" in html
    assert "beta" in html
    assert "gamma" not in html  # cross-host agent hidden from an ordinary caller


def test_fleet_page_crosshost_operator_sees_all(client, fake_fleet, crosshost_operator):
    resp = client.get("/")
    html = resp.content.decode()
    assert "alpha" in html and "gamma" in html
    assert "cross-host" in html  # flagged


def test_fleet_api_scopes_to_identity(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = client.get("/api/fleet")
    assert resp.status_code == 200
    data = json.loads(resp.content)
    assert data["ok"] is True
    names = {a["name"] for a in data["agents"]}
    assert names == {"alpha", "beta"}


def test_fleet_api_reports_listener_unavailable(client, monkeypatch, tmp_path):
    # Point the fleet at a dead base URL and no token: the view must surface a
    # STATE (502), not crash.
    import scitex_agent_container._django._remote as _remote
    import scitex_agent_container._django.views as views

    class DeadFleet:
        base_url = "http://127.0.0.1:9"

        def list_all(self):
            raise _remote.FleetUnavailableError(self.base_url, "connection refused")

    def _from_env(*a, **k):
        return DeadFleet()

    monkeypatch.setattr(views.RemoteFleet, "from_environment", _from_env)
    resp = client.get("/api/fleet")
    assert resp.status_code == 502
    assert json.loads(resp.content)["ok"] is False


def test_detail_own_agent_shows_state(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = client.get("/alpha/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "alpha" in html
    assert "Alive" in html


def test_detail_cross_agent_hidden_from_ordinary(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = client.get("/gamma/")
    assert resp.status_code == 200
    assert "not in scope" in resp.content.decode() or "not visible" in resp.content.decode()


def test_lifecycle_denied_for_non_operator(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = client.post("/alpha/action", {"action": "stop"})
    assert resp.status_code == 403
    # The view may list agents to scope the request, but must not delegate any
    # lifecycle mutation to a non-operator.
    assert all(c[0] != "POST/DELETE" for c in fake_fleet.calls)


def test_lifecycle_own_allowed_for_operator(client, fake_fleet, operator, audit_log):
    resp = client.post("/alpha/action", {"action": "stop"})
    assert resp.status_code in (302, 303)
    assert any(c[0] in ("POST/DELETE",) and c[1] == "alpha" for c in fake_fleet.calls)
    log = json.loads(audit_log.read_text().strip().splitlines()[-1])
    assert log["event"] == "lifecycle_action"
    assert log["agent"] == "alpha"
    assert log["cross_host"] is False


def test_lifecycle_local_operator_can_control_own_agent(client, fake_fleet, audit_log, monkeypatch):
    # An identity in the local lifecycle-operator list controls OWN agents, and
    # that grant is audited with cross_host=False.
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", "localop")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", "")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "localop")
    resp = client.post("/alpha/action", {"action": "restart"})
    assert resp.status_code in (302, 303)
    recs = [json.loads(line) for line in audit_log.read_text().strip().splitlines()]
    assert any(r["event"] == "lifecycle_action" and r["agent"] == "alpha" and r["cross_host"] is False for r in recs)


def test_lifecycle_cross_host_granted_is_audited(client, fake_fleet, crosshost_operator, audit_log):
    # The cross-host operator (in the stricter list) controls a REMOTE agent;
    # the grant is audited with cross_host=True.
    resp = client.post("/gamma/action", {"action": "restart"})
    assert resp.status_code in (302, 303)
    recs = [json.loads(line) for line in audit_log.read_text().strip().splitlines()]
    assert any(r["event"] == "lifecycle_action" and r["agent"] == "gamma" and r["cross_host"] is True for r in recs)


def test_lifecycle_cross_host_denied_when_not_strictly_authorized(client, fake_fleet, audit_log, monkeypatch):
    # A local-only operator (not in the cross-host list) cannot see gamma, so
    # an attempt to control it is refused — and the refusal is audited.
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", "localop")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", "crossop")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "localop")
    resp = client.post("/gamma/action", {"action": "restart"})
    assert resp.status_code == 403
    assert all(c[1] != "gamma" for c in fake_fleet.calls)  # nothing delegated
