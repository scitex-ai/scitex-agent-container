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


def _detail(client, env_save_restore, name, partial):
    # Arrange — an ordinary identity sees its own node's agents.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    url = f"/{name}/" + ("?partial=1" if partial else "")
    return client.get(url).content.decode()


def test_detail_partial_renders_fragment_without_shell(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _detail(client, env_save_restore, "alpha", partial=True)
    # Assert
    assert "<html" not in body and "alpha" in body


def test_detail_full_page_keeps_the_shell(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _detail(client, env_save_restore, "alpha", partial=False)
    # Assert
    assert "<html" in body


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
    # Arrange — the tile stays short ("Agent Container"); the page contents
    # carry the full product name (operator 2026-10-23). Seed the snapshot
    # so the content band renders (see above).
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
        assert "<h1" in html and "SciTeX Agent Container" in html
    finally:
        CACHE.clear()
