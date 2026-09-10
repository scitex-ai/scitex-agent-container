"""Detail view: session-log tail (redacted) + resources + lifecycle integration."""

from __future__ import annotations


def test_detail_shows_redacted_session_tail(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = client.get("/alpha/").content.decode()
    # The transcript's user + result lines are summarized and shown...
    assert "user: start the job" in html
    assert "result: finished ok" in html
    # ...and the secret in the assistant line is REDACTED, never leaked.
    assert "sk-abc123DEF456GHI789jkl012" not in html
    assert "[REDACTED]" in html
    assert "Recent session log" in html
    assert "secrets redacted" in html


def test_detail_without_log_says_no_log_not_error(client, fake_fleet, monkeypatch):
    # beta has no TAIL entry -> read_tail raises 404 -> clean "no log" state.
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = client.get("/beta/").content.decode()
    assert "No session log recorded yet." in html
    assert "Log unavailable" not in html


def test_detail_resources_section_renders(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = client.get("/alpha/").content.decode()
    assert "<h2>Resources</h2>" in html
    # alpha pid=111 does not exist in this namespace -> honest state, no fake RSS.
    assert "Resources" in html
    assert "111" in html  # the pid is shown


def test_cross_host_detail_shown_to_authorized_only(client, fake_fleet, crosshost_operator, monkeypatch):
    # The cross-host operator CAN see gamma's detail (with its resource/log
    # sections); an ordinary identity CANNOT.
    html = client.get("/gamma/").content.decode()
    assert "Agent not in scope" not in html
    assert "<h2>Resources</h2>" in html
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html2 = client.get("/gamma/").content.decode()
    assert "Agent not in scope" in html2
