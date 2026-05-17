"""Tests for ``scitex_agent_container.cli_pkg._send.send_to_agent``.

The library-facing helper that backs both the CLI's cross-host branch
and the MCP ``agent_send`` tool. Verifies:

* status="ok" payload shape on a successful sidecar reply
* status="error" when no active state.db row exists
* status="error" when the row has no a2a_port
* status="timeout" when the sidecar exceeds ``timeout_seconds``
* prompt / key mutual exclusion raises ValueError
* cross-host row routes through the ssh:// branch

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. All
collaborators are swapped at the module namespace via real save/restore
context managers, and env mutations follow the same pattern.

STX-TQ007: each test asserts exactly one fact. STX-TQ002: every test
carries Arrange / Act / Assert markers.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from typing import Iterator

import pytest

import scitex_agent_container.cli_pkg._send as _send_mod
from scitex_agent_container.cli_pkg._send import send_to_agent

# ---------------------------------------------------------------------------
# Collaborator swaps (test seams — no mocks, no monkeypatch)
# ---------------------------------------------------------------------------


@contextmanager
def _swap_post_turn(fn) -> Iterator[None]:
    """Replace ``_send._post_turn`` with ``fn`` for the duration of the test."""
    saved = _send_mod._post_turn
    _send_mod._post_turn = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        _send_mod._post_turn = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# state.db isolation fixture: redirect env so the helper sees an empty
# db unless the test seeds rows itself.
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db_env(tmp_path):
    saved_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host = os.environ.get("SAC_HOST")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(tmp_path / "state.db")
    os.environ["SAC_HOST"] = "lead-host"
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        if saved_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host
        importlib.reload(_state_db_mod)


def _seed_local(name: str, a2a_port: int) -> None:
    """Record an active instance row on the current host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host="lead-host", a2a_port=a2a_port)


def _seed_remote(name: str, peer: str, a2a_port: int) -> None:
    """Record an active instance row on a peer host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host=peer, a2a_port=a2a_port)


# ---------------------------------------------------------------------------
# Spec test 1-3: status="ok" on successful reply + response_text present
# ---------------------------------------------------------------------------


def test_agent_send_returns_dict_with_status_field(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("hello", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi")
    # Assert
    assert "status" in result


def test_agent_send_status_ok_on_successful_response(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("hello back", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi")
    # Assert
    assert result["status"] == "ok"


def test_agent_send_returns_response_text_field_on_success(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("the reply", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi")
    # Assert
    assert result["response_text"] == "the reply"


# ---------------------------------------------------------------------------
# Spec test 4: agent not running → status="error"
# ---------------------------------------------------------------------------


def test_agent_send_status_error_when_agent_not_running(state_db_env):
    # Arrange — no rows seeded
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert result["status"] == "error"


def test_agent_send_error_message_when_agent_not_running(state_db_env):
    # Arrange
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert "not running" in result["error"]


# ---------------------------------------------------------------------------
# Spec test 5: slow sidecar → status="timeout"
# ---------------------------------------------------------------------------


def test_agent_send_status_timeout_on_slow_sidecar(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError(f"peer timeout at {url} after {timeout_s:.0f}s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi", timeout_seconds=2)
    # Assert
    assert result["status"] == "timeout"


def test_agent_send_timeout_error_message_quotes_timeout_value(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi", timeout_seconds=2)
    # Assert
    assert "2s" in result["error"]


# ---------------------------------------------------------------------------
# Spec test 6: prompt + key mutually exclusive → ValueError
# ---------------------------------------------------------------------------


def test_agent_send_prompt_and_key_mutually_exclusive(state_db_env):
    # Arrange
    args = dict(prompt="hi", key="ESC")
    # Act
    raised: Exception | None = None
    try:
        send_to_agent("alpha", **args)
    except ValueError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


def test_agent_send_neither_prompt_nor_key_raises_value_error(state_db_env):
    # Arrange
    name = "alpha"
    # Act
    raised: Exception | None = None
    try:
        send_to_agent(name)
    except ValueError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


# ---------------------------------------------------------------------------
# Spec test 7: cross-host row → ssh:// URL used
# ---------------------------------------------------------------------------


def test_agent_send_cross_host_routes_through_ssh(state_db_env):
    # Arrange
    _seed_remote("beta", peer="peer-x", a2a_port=18888)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent("beta", "hi")
    # Assert
    assert captured["url"] == "ssh://peer-x:18888/v1/turn"


def test_agent_send_local_host_uses_loopback_url(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent("alpha", "hi")
    # Assert
    assert captured["url"] == "http://127.0.0.1:12345/v1/turn"


# ---------------------------------------------------------------------------
# Bonus: response_metadata + a2a_port missing branch + non-200 branch
# ---------------------------------------------------------------------------


def test_agent_send_includes_response_metadata_on_success(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("reply", {"exit_after": True})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi")
    # Assert
    assert result["response_metadata"]["name"] == "alpha"


def test_agent_send_error_when_row_has_no_a2a_port(state_db_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="alpha", host="lead-host", a2a_port=None)
    # Act
    result = send_to_agent("alpha", "hi")
    # Assert
    assert result["status"] == "error"


def test_agent_send_error_when_sidecar_returns_non_200(state_db_env):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer returned HTTP 500: internal error")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent("alpha", "hi", timeout_seconds=2)
    # Assert
    assert result["status"] == "error"
