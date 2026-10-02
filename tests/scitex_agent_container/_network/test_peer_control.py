"""Control requests reach only their explicit endpoint and permitted key."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container._network.peer import PeerError, post_control_to_url


@contextmanager
def _control_server(*, response=None, status=200, raw=None):
    requests = []
    reply = (
        {"delivered": True, "receipt": "public-control-receipt"}
        if response is None
        else response
    )
    encoded = json.dumps(reply).encode() if raw is None else raw

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {"path": self.path, "body": json.loads(self.rfile.read(length))}
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/turn", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _control_error(url, key):
    try:
        post_control_to_url(url, key, timeout_s=2)
    except PeerError as error:
        return error
    raise AssertionError("invalid control unexpectedly returned a receipt")


def test_http_control_returns_the_receiving_endpoint_receipt():
    # Arrange
    with _control_server() as (url, _requests):
        # Act
        response = post_control_to_url(url, "Enter", timeout_s=2)
    # Assert
    assert response == {"delivered": True, "receipt": "public-control-receipt"}


@pytest.mark.parametrize("key", ["Enter", "Escape", "ESC", "C-c", "SIGINT"])
def test_http_control_delivers_only_the_explicit_key_to_the_control_endpoint(key):
    # Arrange
    with _control_server() as (url, requests):
        # Act
        post_control_to_url(url, key, timeout_s=2)
    # Assert
    assert requests == [
        {
            "path": "/v1/control",
            "body": {"kind": "control", "action": "ui.key", "key": key},
        }
    ]


@pytest.mark.parametrize("key", ["Down", "Up", "Return", "enter", ""])
def test_unsupported_key_is_refused_before_any_http_request(key):
    # Arrange
    with _control_server() as (url, requests):
        # Act
        _control_error(url, key)
    # Assert
    assert requests == []


def test_non_turn_url_is_refused_before_any_http_request():
    # Arrange
    with _control_server() as (url, requests):
        # Act
        _control_error(url.removesuffix("/turn") + "/control", "Enter")
    # Assert
    assert requests == []


@pytest.mark.parametrize(
    "response", [{"delivered": False}, {"delivered": 1}, {"unrelated": True}, []]
)
def test_http_control_requires_an_explicit_delivered_receipt(response):
    # Arrange
    with _control_server(response=response) as (url, _requests):
        # Act
        error = _control_error(url, "Enter")
    # Assert
    assert "malformed control response" in str(error)


def test_http_control_surfaces_a_receiver_refusal():
    # Arrange
    with _control_server(response={"detail": "public refusal"}, status=403) as (
        url,
        _requests,
    ):
        # Act
        error = _control_error(url, "Enter")
    # Assert
    assert "HTTP 403" in str(error)


def test_http_control_refuses_a_non_json_response():
    # Arrange
    with _control_server(raw=b"public non-json response") as (url, _requests):
        # Act
        error = _control_error(url, "Enter")
    # Assert
    assert "malformed control body" in str(error)


def _ssh_url(url):
    return url.replace("http://127.0.0.1:", "ssh://public-peer.invalid:")


def test_ssh_control_preserves_the_real_receiver_receipt(ssh_http_shim):
    # Arrange
    ssh_http_shim.install()
    with _control_server() as (url, _requests):
        # Act
        response = post_control_to_url(_ssh_url(url), "Enter", timeout_s=2)
    # Assert
    assert response == {"delivered": True, "receipt": "public-control-receipt"}


def test_ssh_control_routes_only_to_the_declared_host(ssh_http_shim):
    # Arrange
    ssh_http_shim.install()
    with _control_server() as (url, _requests):
        # Act
        post_control_to_url(_ssh_url(url), "Enter", timeout_s=2)
    # Assert
    assert [call["host"] for call in ssh_http_shim.invocations()] == [
        "public-peer.invalid"
    ]


def test_ssh_control_reaches_only_the_explicit_endpoint_and_key(ssh_http_shim):
    # Arrange
    ssh_http_shim.install()
    with _control_server() as (url, requests):
        # Act
        post_control_to_url(_ssh_url(url), "Enter", timeout_s=2)
    # Assert
    assert requests == [
        {
            "path": "/v1/control",
            "body": {"kind": "control", "action": "ui.key", "key": "Enter"},
        }
    ]


def test_unsupported_ssh_key_is_refused_before_any_ssh_process(ssh_http_shim):
    # Arrange
    ssh_http_shim.install()
    # Act
    _control_error("ssh://public-peer.invalid:7878/v1/turn", "Down")
    # Assert
    assert ssh_http_shim.invocations() == []


def test_ssh_control_requires_an_explicit_port_before_any_ssh_process(ssh_http_shim):
    # Arrange
    ssh_http_shim.install()
    # Act
    _control_error("ssh://public-peer.invalid/v1/turn", "Enter")
    # Assert
    assert ssh_http_shim.invocations() == []
