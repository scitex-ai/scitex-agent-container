"""Actual urllib/loopback HTTP tests; no real profiles, credentials or upstream."""

import base64
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container._account.provider_usage_http import fetch
from scitex_agent_container._account.provider_usage_inventory import UsageTarget


@pytest.fixture
def http_contract():
    events, replies = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            events.append((self.command, self.path, list(self.headers)))
            status, payload = replies.get(self.path.split("?")[0], (404, {}))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def route(request, timeout):
        path = urllib.parse.urlsplit(request.full_url)
        local = f"http://127.0.0.1:{server.server_port}" + path.path + ("?" + path.query if path.query else "")
        return opener.open(urllib.request.Request(local, headers=dict(request.headers), method=request.method), timeout=timeout)

    try:
        yield replies, events, route
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_go_request_is_read_only_fixed_usage_path(http_contract):
    # Arrange
    replies, events, route = http_contract
    replies["/zen/go/v1/usage"] = (200, {})
    target = UsageTarget("opencode-go", "OPENCODE_GO_API_KEY_1", ["OPENCODE_GO_API_KEY_1"], secret="synthetic")
    # Act
    fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert [(method, path) for method, path, _ in events] == [("GET", "/zen/go/v1/usage")]


@pytest.mark.parametrize("status", [403, 429, 500])
def test_non_auth_http_refusal_stays_distinct(http_contract, status):
    # Arrange
    replies, _, route = http_contract
    replies["/zen/go/v1/usage"] = (status, {"secret": "synthetic"})
    target = UsageTarget("opencode-go", "OPENCODE_GO_API_KEY_1", ["OPENCODE_GO_API_KEY_1"], secret="synthetic")
    # Act
    result = fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert result["error"] == f"http-{status}"


def test_401_is_authentication_refusal(http_contract):
    # Arrange
    replies, _, route = http_contract
    replies["/zen/go/v1/usage"] = (401, {})
    target = UsageTarget("opencode-go", "OPENCODE_GO_API_KEY_1", ["OPENCODE_GO_API_KEY_1"], secret="synthetic")
    # Act
    result = fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert result["error"] == "authentication-refused"


def test_failed_cost_route_preserves_measured_credit(http_contract):
    # Arrange
    replies, _, route = http_contract
    replies["/alpha/billing/credits"] = (200, {"credits": {"monthlyCredits": 10}})
    target = UsageTarget("commandcode", "COMMANDCODE_API_KEY_01", ["COMMANDCODE_API_KEY_01"], secret="synthetic")
    # Act
    result = fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert result["balances"]["monthly_included"] == 10


def test_commandcode_cost_request_keeps_explicit_30day_period(http_contract):
    # Arrange
    _, events, route = http_contract
    target = UsageTarget("commandcode", "COMMANDCODE_API_KEY_01", ["COMMANDCODE_API_KEY_01"], secret="synthetic")
    now = datetime.now(timezone.utc)
    # Act
    fetch(target, time.monotonic() + 1, opener=route, now=now)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(events[1][1]).query)
    # Assert
    assert query["since"] == [(now - timedelta(days=30)).isoformat()]


def test_codex_login_is_read_without_profile_write(http_contract, tmp_path):
    # Arrange
    replies, _, route = http_contract
    replies["/backend-api/wham/usage"] = (200, {})
    auth = tmp_path / "auth.json"
    before = b'{"tokens":{"access_token":"synthetic","account_id":"synthetic-account"}}'
    auth.write_bytes(before)
    target = UsageTarget("openai", "fixture", ["fixture"], auth_path=auth)
    # Act
    fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert auth.read_bytes() == before


def test_returned_metrics_do_not_contain_auth_or_private_body(http_contract):
    # Arrange
    replies, _, route = http_contract
    replies["/zen/go/v1/usage"] = (200, {"access_token": "synthetic-private"})
    target = UsageTarget("opencode-go", "fixture", ["fixture"], secret="synthetic-private")
    # Act
    result = fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert "synthetic-private" not in json.dumps(result)


def test_expired_codex_token_is_not_refreshed_or_queried(http_contract, tmp_path):
    # Arrange
    _, events, route = http_contract
    auth = tmp_path / "auth.json"
    claim = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
    auth.write_text(json.dumps({"tokens": {"access_token": "head." + claim + ".tail", "account_id": "fixture"}}))
    target = UsageTarget("openai", "fixture", ["fixture"], auth_path=auth)
    # Act
    fetch(target, time.monotonic() + 1, opener=route)
    # Assert
    assert events == []
