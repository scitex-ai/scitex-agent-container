"""Tests for hub_client (ZOO#12 lead-state-handover).

No-mocks pattern: ``urllib.request.urlopen`` is replaced by an injected
``opener`` callable (production refactor) — tests pass a hand-rolled
opener that returns canned ``urllib.response``-shaped objects. No
``monkeypatch.setattr`` on production internals.
"""

from __future__ import annotations

import io
import json
from urllib import error as urlerror

import pytest

from scitex_agent_container._network import hub_client

# ---------------------------------------------------------------------------
# Real fake response (no mocks) — has the protocol urllib callers expect.
# ---------------------------------------------------------------------------


class _FakeResp:
    """A real callable response object — matches the urllib contract."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _opener_returning(body: bytes, status: int = 200):
    """Build an opener that captures the request + returns a fixed body."""
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = dict(req.headers)
        captured["timeout"] = timeout
        return _FakeResp(body, status)

    return opener, captured


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


# ---------------------------------------------------------------------------
# Token / URL env fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hub_env(env_save_restore):
    """Set SAC_HUB_TOKEN + SAC_HUB_URL via real env save/restore."""
    env_save_restore.set("SAC_HUB_TOKEN", "wks_test_token")
    env_save_restore.set("SAC_HUB_URL", "https://hub.test")
    # Clear the long-form aliases that _env.getenv() also reads.
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_TOKEN")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_URL")
    yield


# ---------------------------------------------------------------------------
# push_snapshot
# ---------------------------------------------------------------------------


def test_push_snapshot_posts_to_correct_url(hub_env):
    # Arrange
    opener, captured = _opener_returning(b'{"status":"ok"}')
    # Act
    hub_client.push_snapshot("lead", {"memory": "x"}, opener=opener)
    # Assert
    assert captured["url"] == "https://hub.test/api/agents/lead/snapshot/"


def test_push_snapshot_uses_post_method(hub_env):
    # Arrange
    opener, captured = _opener_returning(b'{"status":"ok"}')
    # Act
    hub_client.push_snapshot("lead", {"memory": "x"}, opener=opener)
    # Assert
    assert captured["method"] == "POST"


def test_push_snapshot_includes_token_in_body(hub_env):
    # Arrange
    opener, captured = _opener_returning(b'{"status":"ok"}')
    # Act
    hub_client.push_snapshot("lead", {"memory": "x"}, opener=opener)
    # Assert
    assert json.loads(captured["body"])["token"] == "wks_test_token"


def test_push_snapshot_includes_owner_host(hub_env):
    # Arrange
    opener, captured = _opener_returning(b'{"status":"ok"}')
    # Act
    hub_client.push_snapshot(
        "lead", {"memory": "x"}, owner_host="spartan", opener=opener
    )
    # Assert
    assert json.loads(captured["body"])["owner_host"] == "spartan"


def test_push_snapshot_returns_true_on_ok_status(hub_env):
    # Arrange
    opener, _ = _opener_returning(b'{"status":"ok"}')
    # Act
    ok = hub_client.push_snapshot("lead", {"memory": "x"}, opener=opener)
    # Assert
    assert ok is True


def test_push_snapshot_returns_false_on_http_error(hub_env):
    # Arrange
    opener = _opener_raising(
        urlerror.HTTPError(
            "https://hub.test/api/agents/lead/snapshot/",
            413,
            "Payload Too Large",
            {},
            io.BytesIO(b"too big"),
        )
    )
    # Act
    ok = hub_client.push_snapshot("lead", {"x": 1}, opener=opener)
    # Assert
    assert ok is False


def test_push_snapshot_no_token_returns_false(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_HUB_URL", "https://hub.test")
    env_save_restore.set("SAC_HUB_TOKEN", "")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_TOKEN")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_URL")

    def must_not_call(req, timeout=None):
        raise AssertionError("opener called when token is empty")

    # Act
    ok = hub_client.push_snapshot("lead", {"x": 1}, opener=must_not_call)
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# fetch_snapshot
# ---------------------------------------------------------------------------


def test_fetch_snapshot_uses_get_method(hub_env):
    # Arrange
    body = json.dumps({"agent_name": "lead", "payload": {}}).encode()
    opener, captured = _opener_returning(body)
    # Act
    hub_client.fetch_snapshot("lead", opener=opener)
    # Assert
    assert captured["method"] == "GET"


def test_fetch_snapshot_appends_token_in_query_string(hub_env):
    # Arrange
    body = json.dumps({"agent_name": "lead", "payload": {}}).encode()
    opener, captured = _opener_returning(body)
    # Act
    hub_client.fetch_snapshot("lead", opener=opener)
    # Assert
    assert "token=wks_test_token" in captured["url"]


def test_fetch_snapshot_returns_payload_dict(hub_env):
    # Arrange
    body = json.dumps(
        {"agent_name": "lead", "owner_host": "spartan", "payload": {"k": "v"}}
    ).encode()
    opener, _ = _opener_returning(body)
    # Act
    out = hub_client.fetch_snapshot("lead", opener=opener)
    # Assert
    assert out is not None and out["payload"] == {"k": "v"}


def test_fetch_snapshot_404_returns_none(hub_env):
    # Arrange
    opener = _opener_raising(
        urlerror.HTTPError("https://hub.test/x", 404, "no", {}, io.BytesIO(b""))
    )
    # Act
    out = hub_client.fetch_snapshot("ghost", opener=opener)
    # Assert
    assert out is None


# ---------------------------------------------------------------------------
# fetch_owner
# ---------------------------------------------------------------------------


def test_fetch_owner_returns_priority_list(hub_env):
    # Arrange
    body = json.dumps(
        {
            "agent": "lead",
            "current_host": "spartan",
            "priority_list": ["spartan", "mba"],
            "healthy": {"spartan": True, "mba": False},
        }
    ).encode()
    opener, _ = _opener_returning(body)
    # Act
    out = hub_client.fetch_owner("lead", opener=opener)
    # Assert
    assert out["priority_list"] == ["spartan", "mba"]


def test_fetch_owner_returns_healthy_map(hub_env):
    # Arrange
    body = json.dumps(
        {
            "agent": "lead",
            "current_host": "spartan",
            "priority_list": [],
            "healthy": {"spartan": True, "mba": False},
        }
    ).encode()
    opener, _ = _opener_returning(body)
    # Act
    out = hub_client.fetch_owner("lead", opener=opener)
    # Assert
    assert out["healthy"] == {"spartan": True, "mba": False}


def test_fetch_owner_url_error_returns_empty_priority_list(hub_env):
    # Arrange
    opener = _opener_raising(urlerror.URLError("nope"))
    # Act
    out = hub_client.fetch_owner("lead", opener=opener)
    # Assert
    assert out["priority_list"] == []


def test_fetch_owner_url_error_returns_empty_healthy_map(hub_env):
    # Arrange
    opener = _opener_raising(urlerror.URLError("nope"))
    # Act
    out = hub_client.fetch_owner("lead", opener=opener)
    # Assert
    assert out["healthy"] == {}


# ---------------------------------------------------------------------------
# _request default-opener and short-circuit branches
# ---------------------------------------------------------------------------


def test_request_returns_none_when_hub_url_unset(env_save_restore):
    # Arrange no SAC_HUB_URL — _request must short-circuit before opener.
    env_save_restore.set("SAC_HUB_URL", "")
    env_save_restore.set("SAC_HUB_TOKEN", "wks_t")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_URL")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HUB_TOKEN")
    # Act default-opener path: opener=None must not crash because we
    # return before any urlopen call.
    out = hub_client._request("GET", "/api/agents/x/snapshot/latest/")
    # Assert
    assert out is None


def test_request_returns_empty_dict_on_empty_response_body(hub_env):
    # Arrange a 200 with no body — _request must return {} sentinel.
    opener, _ = _opener_returning(b"")
    # Act
    out = hub_client._request("GET", "/api/agents/x/owner/", opener=opener)
    # Assert
    assert out == {}
