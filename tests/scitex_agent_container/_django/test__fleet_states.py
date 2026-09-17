"""The five-state read contract for the Agents fleet page.

The card requires the surface to tell these apart instead of blending them into
one banner, and to keep the failure states actionable:

    loading | empty | setup-required | denied | unavailable

Design constraints this file pins down:

* **loading is not a server state.** The fleet read is server-rendered, so the
  page boots already-resolved; the only thing a visitor can see before the first
  paint is the browser's own navigation. Recording that honestly means the
  skeleton markup exists in the document AND the finished state ships in it.
* **denied vs empty is a security distinction, not a cosmetic one.** A caller
  whose identity is refused must never be able to infer fleet contents from an
  empty-looking page. Both render zero rows; only the copy differs, and the
  denied copy asserts nothing about how many agents exist.
* **unavailable is not a crash.** It is a state with a Retry the operator can
  actually use, because the upstream listener is reachable-but-slow in normal
  operation (measured 7.7-42.4s).
* **the diagnostic ID is sanitized.** It must identify the incident for support
  without carrying a token, a URL with credentials, a host path, or any
  agent-identifying detail. It is derived (hashed), never a raw error string.
"""

from __future__ import annotations

import re

from scitex_agent_container._django._constants import (
    IDENTITY_ENV,
    OPERATORS_ENV,
)
from scitex_agent_container._django._diagnostics import diagnostic_id

# Every state the card names, as rendered markers (data-state on the app root).
STATE_ATTR = 'data-fleet-state'


def _state(html: str) -> str:
    match = re.search(rf'{STATE_ATTR}="([a-z-]+)"', html)
    assert match, f"no {STATE_ATTR} on the app root: {html[:400]}"
    return match.group(1)


def _fleet(client) -> str:
    return client.get("/").content.decode()


# ── loading ──────────────────────────────────────────────────────────────────


def test_loading_skeleton_exists_and_resolved_state_supersedes_it(client, loopback):
    # Arrange / Act
    html = _fleet(client)
    # Assert: the skeleton markup ships in the document AND a resolved state
    # ships in the SAME response. This surface is server-rendered, so it cannot
    # linger in "loading" — the skeleton covers the browser's navigation window
    # only, and claiming more than that would be a fiction about how it boots.
    assert "agents-skeleton" in html and f'{STATE_ATTR}="ok"' in html


# ── empty ────────────────────────────────────────────────────────────────────


def test_empty_scope_is_explicit_not_an_error(client, loopback, env_save_restore):
    # Arrange: the listener answers, with a zero-length fleet.
    from .conftest import _Listener

    _Listener.agents_override = []
    env_save_restore.set(IDENTITY_ENV, "alice")
    try:
        # Act
        html = _fleet(client)
        # Assert
        assert _state(html) == "empty" and "No agents" in html
    finally:
        _Listener.agents_override = None


# ── setup-required ───────────────────────────────────────────────────────────


def test_missing_configuration_is_setup_required(client, env_save_restore):
    # Arrange: a deployment that has chosen NO listener. `_remote` falls back to
    # DEFAULT_API_URL (127.0.0.1:7878) when none is set, and on a compute node
    # that default IS a real, answering listener — so "URL unset" alone is not
    # the same as "unconfigured", and the two must not be conflated. Point the
    # default at a closed port to model a host with nothing on it.
    from scitex_agent_container._django import _remote

    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", "http://127.0.0.1:1")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_API_TOKEN")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_API_TOKEN_FILE")
    original_default = _remote.DEFAULT_API_URL
    _remote.DEFAULT_API_URL = "http://127.0.0.1:1"
    try:
        # Act: no operator-chosen URL, and the default resolves nowhere.
        env_save_restore.delete("SCITEX_AGENT_CONTAINER_API_URL")
        html = _fleet(client)
    finally:
        _remote.DEFAULT_API_URL = original_default
    # Assert
    assert _state(html) == "setup-required"


# ── unavailable ──────────────────────────────────────────────────────────────


def test_configured_but_unreachable_is_unavailable_not_setup(client, env_save_restore):
    # Arrange: configuration present, listener pointed at a closed port.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", "http://127.0.0.1:1")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "tok")
    # Act
    html = _fleet(client)
    # Assert
    assert _state(html) == "unavailable"


def test_unavailable_offers_a_retry_control(client, env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", "http://127.0.0.1:1")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "tok")
    # Act
    html = _fleet(client)
    # Assert
    assert 'data-action="retry"' in html and "Retry" in html


def test_unavailable_states_carry_a_sanitized_diagnostic_id(client, env_save_restore):
    # Arrange: a failure whose message contains a secret and a host path.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", "http://127.0.0.1:1")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "super-secret-token-value")
    # Act
    html = _fleet(client)
    # Assert: an ID is shown, and neither the token nor a filesystem path leaks.
    assert re.search(r"diag-[0-9a-f]{12}", html)
    assert "super-secret-token-value" not in html
    assert "/home/" not in html and "/scratch/" not in html


# ── denied ───────────────────────────────────────────────────────────────────


def test_denied_is_an_authorization_outcome_not_a_read_outcome(client, loopback, env_save_restore):
    # Arrange: a caller with NO identity reading a fleet it is not authorized to
    # act on. The book it may READ is not the set it may CONTROL, so the fleet
    # still renders honestly as `ok` — `denied` must not be manufactured from the
    # read path, or it would either mask a working fleet or leak its size.
    env_save_restore.delete(IDENTITY_ENV)
    env_save_restore.delete(OPERATORS_ENV)
    # Act
    html = _fleet(client)
    # Assert: readable and authorized are different questions.
    assert _state(html) == "ok" and "own-scope only" in html


def test_denied_state_renders_and_asserts_nothing_about_the_fleet(client, loopback, env_save_restore):
    # Arrange: the lifecycle_action refusal path is the one place `denied` is a
    # real outcome. Reaching it with a cross-host row and a caller who is not a
    # cross-host operator proves the refused page describes no fleet contents.
    from scitex_agent_container._django._constants import CROSSHOST_OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "stranger")
    env_save_restore.delete(CROSSHOST_OPERATORS_ENV)
    # Act: a POST to a cross-host agent's action route, refused on authorization.
    response = client.post("/gamma/action", {"action": "stop"})
    # Assert: refused WITHOUT executing anything, and the copy names no agent.
    body = response.content.decode().lower()
    assert response.status_code == 403 and "gamma" not in body


# ── the diagnostic ID itself ─────────────────────────────────────────────────


def test_diagnostic_id_is_stable_and_sanitized():
    # Arrange / Act
    first = diagnostic_id("could not reach http://user:pw@h/x?token=abc")
    second = diagnostic_id("could not reach http://user:pw@h/x?token=abc")
    other = diagnostic_id("something else entirely")
    # Assert: same input -> same ID (support can correlate across a page reload),
    # different input -> different ID, and the ID carries no input text.
    assert first == second and first != other
    assert re.fullmatch(r"diag-[0-9a-f]{12}", first)
    assert "token" not in first and "pw" not in first
