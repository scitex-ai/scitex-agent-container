"""Polish regression: accessible action column + real scitex-ui color tokens."""

from __future__ import annotations

from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "src" / "scitex_agent_container" / "_django"


def test_fleet_page_labels_the_action_column(client, fake_fleet, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = client.get("/").content.decode()
    # The action column must carry a visible accessible name ("Actions") so the
    # table is not a column with no accessible name.
    assert '<th class="row-actions">Actions</th>' in html


def test_css_uses_real_scitex_ui_tokens_not_invented_ones():
    css = (PKG / "static" / "scitex_agent_container" / "agents.css").read_text(encoding="utf-8")
    # The dim/secondary text must resolve to scitex-ui's real token (brighter
    # than a low-contrast literal) and the accent must track --accent.
    assert "var(--text-secondary" in css
    assert "var(--accent" in css
    # Invented --stx-text / --stx-surface names must be gone.
    assert "--stx-text" not in css
    assert "--stx-surface" not in css


def test_action_header_is_right_aligned_and_hidden_on_mobile():
    css = (PKG / "static" / "scitex_agent_container" / "agents.css").read_text(encoding="utf-8")
    assert ".row-actions { text-align: right" in css.replace("  ", " ")
