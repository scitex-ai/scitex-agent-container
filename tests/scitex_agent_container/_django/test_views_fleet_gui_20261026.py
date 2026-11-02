"""Tests for the 2026-10-26 fleet GUI operator batch.

DM composer + Details column (card sac-gui-fleet-dm-details-20261026):
(1) no "Messaging <target>" div under the input, (2) Enter-to-send stays
on the fleet page (fetch POST, JSON outcome), (3) Details keeps its
5 tabs, (4) tabs use the shared SDK underscore contract.

Render tests run against the real HTTP surface with a seeded snapshot
(no browser needed); JS behaviour is pinned by source assertions the
2026-10-22 module established. Each test has AAA markers and a single
assertion.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

LEAF = Path(__file__).resolve().parents[3] / "src/scitex_agent_container/_django"
CSS = (LEAF / "static/scitex_agent_container/agents.css").read_text(encoding="utf-8")
FLEET_JS = (LEAF / "static/scitex_agent_container/js/fleet-details.js").read_text(
    encoding="utf-8"
)
FLEET_HTML = (LEAF / "templates/scitex_agent_container/_fleet_content.html").read_text(
    encoding="utf-8"
)

ALPHA_ROW = {
    "name": "alpha",
    "state_label": "Alive",
    "state_tone": "good",
    "runtime": "apptainer",
    "harness": "x",
    "role": "worker",
    "engine": "e",
    "model": "m",
    "project": "p",
    "host": "h",
    "scope": "own",
    "cross_host": False,
    "a2a_port": 19000,
    "pid": 1,
    "activity": {},
}


def _seeded_fleet_html(client, env_save_restore, identity="alice"):
    # Arrange — seed the snapshot the shell renders from.
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put(identity, [dict(ALPHA_ROW)])
    env_save_restore.set(IDENTITY_ENV, identity)
    try:
        # Act
        return client.get("/").content.decode()
    finally:
        CACHE.clear()


# ── item 1: no "Messaging <target>" div ─────────────────────────────────────
def test_fleet_shell_has_no_messaging_hint_div(client, env_save_restore):
    # Arrange — seeded fleet snapshot (helper renders the shell).
    # Act
    html = _seeded_fleet_html(client, env_save_restore)
    # Assert — the composer keeps its placeholder; the second row is gone.
    assert "fleet-composer-hint" not in html and 'name="message"' in html


def test_messaging_hint_gone_from_all_three_surfaces():
    # Arrange — template + CSS + JS must agree; a leftover reference in any
    # one of them is a dead hook or a resurrected row.
    # Act
    surfaces = FLEET_HTML + CSS + FLEET_JS
    # Assert
    assert "fleet-composer-hint" not in surfaces and "Messaging " not in FLEET_JS


# ── item 2: Enter-to-send stays on the fleet page ────────────────────────────
def test_message_refusal_stays_redirect_without_json_accept(
    client, loopback, env_save_restore
):
    # Arrange — an authorized operator, both fields set, no JSON asked.
    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post("/alpha/message", {"message": "hi", "control_key": "Enter"})
    # Assert — the established redirect-to-detail behaviour is unchanged.
    assert response.status_code == 302 and "state=refused" in response["Location"]


def test_message_refusal_returns_json_when_accepted(client, loopback, env_save_restore):
    # Arrange — same refusal, but the fleet composer asks for JSON.
    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post(
        "/alpha/message",
        {"message": "hi", "control_key": "Enter"},
        HTTP_ACCEPT="application/json",
    )
    # Assert — no redirect: the JSON outcome stays on the fleet page.
    assert response.status_code == 400 and response.json() == {
        "ok": False,
        "error": "Provide either a message or a single control key.",
    }


def test_fleet_composer_posts_via_fetch_without_native_fallback():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — a delegated submit listener POSTs the fleet composer via fetch
    # (asking for JSON); an unconfirmed outcome keeps the draft inline.
    assert (
        'addEventListener("submit"' in js
        and "new FormData(form)" in js
        and "composerError(form" in js
        and "else composer.submit()" not in js
    )


# ── item 3: Details keeps its 5 tabs ─────────────────────────────────────────
def test_detail_fragment_still_renders_five_tabs(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPERATORS_ENV, "op1")
    # Act
    body = client.get("/alpha/?partial=1").content.decode()
    # Assert — the exact five-tab set the operator named, nothing dropped.
    assert body.count('role="tab"') == 5 and all(
        label in body
        for label in (
            ">Identity<",
            ">Runtime Activity<",
            ">Control<",
            ">Resources<",
            ">Recent Session Log<",
        )
    )


# ── item 4: SDK underscore tab contract ──────────────────────────────────────
def test_detail_tabs_use_sdk_underline_selected_state():
    # Arrange — file content only: the selected tab must read as the shared
    # stx-content-tabs contract (underline only, accent token), never a box.
    # Act
    selected = CSS.split(".detail-tab--active")[1].split("}")[0]
    base = CSS.split(".detail-tab {")[1].split("}")[0]
    # Assert
    assert (
        "border-bottom: 2px solid var(--agents-cross)" in selected
        and "border-color: transparent" in selected
        and "font-size: 13px" in base
        and "text-transform" not in base
    )
