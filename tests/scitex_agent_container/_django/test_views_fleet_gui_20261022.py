"""Tests for the 2026-10-22 fleet GUI prompt (items 1-3 + 5).

TDD RED-first regressions against the real loopback listener and the real
HTTP surface (no mocks). Each test has AAA markers and a single assertion.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

LEAF = Path(__file__).resolve().parents[3] / "src/scitex_agent_container/_django"
CSS = (LEAF / "static/scitex_agent_container/agents.css").read_text(encoding="utf-8")
FLEET_JS = (LEAF / "static/scitex_agent_container/js/fleet-details.js").read_text(
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
    # Arrange — seed the snapshot the shell renders from (cold cache would
    # render the loading state with no table, same as the real first paint).
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put(identity, [dict(ALPHA_ROW)])
    env_save_restore.set(IDENTITY_ENV, identity)
    try:
        # Act
        return client.get("/").content.decode()
    finally:
        CACHE.clear()


# ── item 1: always-visible fleet DM composer ────────────────────────────────
def test_fleet_center_has_always_visible_dm_composer(client, env_save_restore):
    # Arrange — seeded fleet snapshot (helper renders the shell).
    # Act
    html = _seeded_fleet_html(client, env_save_restore)
    # Assert — the skeleton the JS retargets: same POST shape as the detail
    # message-form (message + dispatch_id + emoji + send), fleet-scoped hooks.
    assert (
        'data-role="fleet-composer"' in html
        and 'data-composer="fleet-dm"' in html
        and 'name="message"' in html
        and 'name="dispatch_id"' in html
        and "data-emoji-button" in html
    )


def test_fleet_composer_js_retargets_on_row_select():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — the fleet composer is retargeted (form action + textarea) when
    # a row is selected, instead of adopting the detail fragment's composer.
    assert "retargetFleetComposer" in js and "fleet-dm" in js


# ── item 2: pagehead row + pane heights ─────────────────────────────────────
def test_css_pagehead_row_shares_one_height():
    # Arrange — file content only.
    # Act
    css = CSS
    # Assert — buttons and sum-cards share one height with equal top/bottom
    # margins, and the leftmost child adds no extra left margin.
    assert (
        ".agents-pagehead .btn, .agents-pagehead .sum-card" in css
        and "margin-left: 0" in css
    )


def test_css_row_height_clears_dock():
    # Arrange — file content only.
    # Act
    css = CSS
    # Assert — the 280px reserve is gone (180px range), 300px floor kept.
    assert (
        "calc(100vh - 180px)" in css
        and "calc(100vh - 280px)" not in css
        and "min-height: 300px" in css
    )


def test_css_composer_buttons_share_height():
    # Arrange — file content only.
    # Act
    css = CSS
    # Assert — the emoji + send 44px shared-height rule still exists.
    assert ".composer-actions .btn" in css and "height: 44px" in css


# ── item 3: detail fragment tabs ─────────────────────────────────────────────
def test_detail_fragment_renders_five_tabs_not_details(
    client, loopback, env_save_restore
):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPERATORS_ENV, "op1")
    # Act
    body = client.get("/alpha/?partial=1").content.decode()
    # Assert — 5 tabs in a tablist; the standalone Message thread card and
    # every <details> shell are gone (thread lives in the center DM pane).
    assert (
        body.count('role="tab"') == 5
        and 'role="tablist"' in body
        and 'id="message-thread"' not in body
        and "<details" not in body
    )


def test_detail_tabs_keep_post_forms_identical(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPERATORS_ENV, "op1")
    # Act
    body = client.get("/alpha/?partial=1").content.decode()
    # Assert — lifecycle + message + control POST endpoints unchanged.
    assert (
        'action="/alpha/action"' in body
        and 'action="/alpha/message"' in body
        and 'name="dispatch_id"' in body
        and 'name="control_key"' in body
    )


def test_detail_tabs_nojs_fallback_shows_all_panels(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPERATORS_ENV, "op1")
    # Act
    body = client.get("/alpha/?partial=1").content.decode()
    # Assert — server HTML hides nothing (JS activates the first tab); all
    # five panels render for no-JS readers.
    assert body.count('role="tabpanel"') == 5 and not re.search(
        r"<[^>]*\shidden(?:\s|>|/>)", body
    )


# ── item 5: bounded cold-shell retry ─────────────────────────────────────────
def test_listener_down_settles_unavailable_within_two_paints(
    client, unreachable_listener, env_save_restore
):
    # Arrange — a fresh identity whose last-known snapshot does not exist yet.
    from scitex_agent_container._django._inventory_cache import CACHE

    identity = "probe-bounded-retry-1"
    env_save_restore.set(IDENTITY_ENV, identity)
    # Act — first paint is loading; the background refresh then records the
    # completed failure; the second paint must show unavailable + Retry.
    first = client.get("/").content.decode()
    deadline = time.time() + 20
    while CACHE.get(identity) is None and time.time() < deadline:
        time.sleep(0.05)
    second = client.get("/").content.decode()
    # Assert
    assert (
        'data-fleet-state="loading"' in first
        and 'data-fleet-state="unavailable"' in second
        and "Retry" in second
    )


def test_loading_shell_noscript_offers_retry(client, unreachable_listener, env_save_restore):
    # Arrange — a fresh identity: cold cache, unreachable control plane.
    env_save_restore.set(IDENTITY_ENV, "probe-bounded-retry-2")
    # Act
    html = client.get("/").content.decode()
    # Assert — hard-reload (no-JS) users still get a Retry path out of loading.
    assert "<noscript>" in html and "Retry" in html


def test_warm_poll_is_bounded_without_reload_loops():
    # Arrange — static JS only, no browser needed.
    # Act
    js = FLEET_JS
    # Assert — the cold poll is bounded (not one-shot-then-reload-forever)
    # and settles in place instead of reloading more than twice.
    assert (
        "COLD_POLL_MAX" in js
        and "settleUnavailable" in js
        and js.count("window.location.reload") <= 2
    )
