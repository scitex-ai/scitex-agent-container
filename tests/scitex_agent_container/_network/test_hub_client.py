"""Tests for hub_client (ZOO#12 lead-state-handover).

Verifies the stdlib HTTP wrappers without touching the real hub:
``urllib.request.urlopen`` is monkeypatched to return canned responses
or raise ``HTTPError`` / ``URLError`` so the error-swallow paths are
exercised too. Mirrors the pattern in ``tests/test_hooks.py`` for the
``hooks._dispatch_http`` HTTP path.
"""

from __future__ import annotations

import io
import json
from urllib import error as urlerror

import pytest

from scitex_agent_container._network import hub_client


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_HUB_TOKEN", "wks_test_token")
    monkeypatch.setenv("SCITEX_AGENT_HUB_URL", "https://hub.test")


def test_push_snapshot_posts_with_token(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = dict(req.headers)
        return _FakeResp(b'{"status":"ok","bytes":42,"agent_name":"lead"}')

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", fake_urlopen)
    ok = hub_client.push_snapshot("lead", {"memory": "x"}, owner_host="spartan")
    assert ok is True
    assert captured["url"] == "https://hub.test/api/agents/lead/snapshot/"
    assert captured["method"] == "POST"
    body = json.loads(captured["body"])
    assert body["token"] == "wks_test_token"
    assert body["owner_host"] == "spartan"
    assert body["payload"] == {"memory": "x"}


def test_push_snapshot_returns_false_on_http_error(monkeypatch):
    def boom(req, timeout=None):
        raise urlerror.HTTPError(
            req.full_url, 413, "Payload Too Large", {}, io.BytesIO(b"too big")
        )

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", boom)
    assert hub_client.push_snapshot("lead", {"x": 1}) is False


def test_push_snapshot_no_token_is_noop(monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_HUB_TOKEN", "")

    def must_not_be_called(*a, **kw):
        raise AssertionError("urlopen called when token is empty")

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", must_not_be_called)
    assert hub_client.push_snapshot("lead", {"x": 1}) is False


def test_fetch_snapshot_returns_payload(monkeypatch):
    def fake(req, timeout=None):
        assert req.get_method() == "GET"
        assert "token=wks_test_token" in req.full_url
        return _FakeResp(
            json.dumps(
                {"agent_name": "lead", "owner_host": "spartan", "payload": {"k": "v"}}
            ).encode("utf-8")
        )

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", fake)
    out = hub_client.fetch_snapshot("lead")
    assert out["payload"] == {"k": "v"}
    assert out["owner_host"] == "spartan"


def test_fetch_snapshot_404_returns_none(monkeypatch):
    def boom(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 404, "no", {}, io.BytesIO(b""))

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", boom)
    assert hub_client.fetch_snapshot("ghost") is None


def test_fetch_owner_returns_payload(monkeypatch):
    def fake(req, timeout=None):
        return _FakeResp(
            json.dumps(
                {
                    "agent": "lead",
                    "current_host": "spartan",
                    "priority_list": ["spartan", "mba"],
                    "healthy": {"spartan": True, "mba": False},
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", fake)
    out = hub_client.fetch_owner("lead")
    assert out["priority_list"] == ["spartan", "mba"]
    assert out["healthy"] == {"spartan": True, "mba": False}


def test_fetch_owner_url_error_returns_empty(monkeypatch):
    def boom(req, timeout=None):
        raise urlerror.URLError("nope")

    monkeypatch.setattr(hub_client.urlrequest, "urlopen", boom)
    out = hub_client.fetch_owner("lead")
    # On error: dict is returned with empty values so callers don't NPE.
    assert out["priority_list"] == []
    assert out["healthy"] == {}
    assert out["current_host"] == ""
