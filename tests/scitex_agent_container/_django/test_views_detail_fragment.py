"""Tests for the fleet page's dynamic Details pane (operator 2026-10-23).

Agent details live in the shared Details column, filled when a row is
selected: the fleet page carries the SDK panes layout (fleet list +
details pane), row clicks fetch the same detail view with ``?partial=1``,
and the view renders the ``_detail_content`` partial only — no shell,
no <html> wrapper. Full-page loads are unchanged.

End to end against the REAL loopback listener; no mocks, no monkeypatch
(STX-NM002 / PA-306). Each test has AAA markers and a single assertion
(STX-TQ002/TQ007).
"""

from __future__ import annotations

from scitex_agent_container._django._constants import IDENTITY_ENV


def _detail(client, env_save_restore, name, partial, identity="alice"):
    # Arrange — an ordinary identity sees its own node's agents.
    env_save_restore.set(IDENTITY_ENV, identity)
    # Act
    url = f"/{name}/" + ("?partial=1" if partial else "")
    return client.get(url).content.decode()


def test_detail_partial_renders_fragment_without_shell(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _detail(client, env_save_restore, "alpha", partial=True)
    # Assert — no <html> shell; the agent name is NOT repeated in the
    # fragment (operator 2026-10-26: the roster selection already names
    # it, and the DM/Details panes must not triple it).
    assert "<html" not in body and "detail-card" in body
    assert "detail-name" not in body


def test_detail_full_page_keeps_the_shell(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _detail(client, env_save_restore, "alpha", partial=False)
    # Assert — full page keeps the shell AND the name heading (the name is
    # only dropped from the fleet-pane fragment).
    assert "<html" in body and "detail-name" in body and "alpha" in body


def test_fleet_page_mounts_panes_with_fleet_and_details(client, loopback, env_save_restore):
    # Arrange — seed the snapshot the shell renders from (cold cache would
    # render the loading state with no table, same as the real first paint
    # before the background poll warms it).
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put("alice", [{"name": "alpha", "state_label": "Alive", "state_tone": "good",
                         "runtime": "apptainer", "harness": "x", "role": "worker",
                         "engine": "e", "model": "m", "project": "p",
                         "host": "h", "scope": "own", "cross_host": False,
                         "a2a_port": 19000, "pid": 1, "activity": {}}])
    env_save_restore.set(IDENTITY_ENV, "alice")
    try:
        # Act
        html = client.get("/").content.decode()
        # Assert
        assert 'data-stx-panes="agents"' in html and 'data-role="fleet-detail"' in html
    finally:
        CACHE.clear()


def test_fleet_page_names_the_full_product_in_contents(client, loopback, env_save_restore):
    # Arrange — the tile stays short ("Agent Container"); the page header
    # carries the full product name (operator 2026-10-23, Scholar/Stats
    # precedent). Seed the snapshot so the content band renders (see above).
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put("alice", [{"name": "alpha", "state_label": "Alive", "state_tone": "good",
                         "runtime": "apptainer", "harness": "x", "role": "worker",
                         "engine": "e", "model": "m", "project": "p",
                         "host": "h", "scope": "own", "cross_host": False,
                         "a2a_port": 19000, "pid": 1, "activity": {}}])
    env_save_restore.set(IDENTITY_ENV, "alice")
    try:
        # Act
        html = client.get("/").content.decode()
        # Assert — operator 2026-10-23: no duplicate pagehead h1; the SDK
        # header row renders the full product name "SciTeX Agent Container".
        assert "<h1" not in html and "SciTeX Agent Container" in html
    finally:
        CACHE.clear()


def test_dm_composer_marks_enter_to_send_and_emoji_hooks(client, loopback, env_save_restore):
    # Arrange
    from scitex_agent_container._django._constants import (
        IDENTITY_ENV,
        OPERATORS_ENV,
    )

    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPERATORS_ENV, "op1")
    # Act
    body = _detail(client, env_save_restore, "alpha", partial=True, identity="op1")
    # Assert — one behavior contract: the composer exposes the hooks the
    # delegated fleet JS binds (Enter-to-send + emoji stamp).
    assert 'data-composer="dm"' in body and 'data-emoji-button' in body
