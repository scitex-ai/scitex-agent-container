"""Tests for the compass L518 advanced-runtime toggle.

Compass §15 L518: hide advanced runtime details (pid / a2a port / turn URL /
session) until needed — a native <details> disclosure in the Identity tab,
collapsed by default, no-JS safe.

End to end against the REAL loopback listener; no mocks, no monkeypatch
(STX-NM002 / PA-306). Each test has AAA markers and a single assertion
(STX-TQ002/TQ007).
"""

from __future__ import annotations

from scitex_agent_container._django._constants import IDENTITY_ENV


def _fragment(client, env_save_restore, name="alpha"):
    # Arrange — an ordinary identity sees its own node's agents.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    return client.get(f"/{name}/?partial=1").content.decode()


def test_detail_identity_pid_sits_inside_advanced_toggle(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _fragment(client, env_save_restore)
    # Assert — the PID fact renders after the toggle opens, not in the top-level facts.
    assert body.index('data-advanced="runtime"') < body.index("<dt>PID</dt>")


def test_detail_identity_advanced_toggle_is_collapsed_by_default(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _fragment(client, env_save_restore)
    # Assert — the exact open tag carries no open attribute.
    assert '<details class="detail-advanced" data-advanced="runtime">' in body


def test_detail_identity_model_stays_outside_advanced_toggle(client, loopback, env_save_restore):
    # Arrange
    # Act
    body = _fragment(client, env_save_restore)
    # Assert — everyday facts render before the advanced disclosure.
    assert body.index("<dt>Model</dt>") < body.index('data-advanced="runtime"')


def test_detail_full_page_shares_the_advanced_toggle(client, loopback, env_save_restore):
    # Arrange — the full page wraps the same content partial.
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    body = client.get("/alpha/").content.decode()
    # Assert — positive control: both render paths carry the toggle.
    assert '<details class="detail-advanced" data-advanced="runtime">' in body
