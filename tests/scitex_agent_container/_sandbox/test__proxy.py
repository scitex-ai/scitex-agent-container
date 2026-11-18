"""Host egress proxy: allowlist CONNECT, refusal logging, gateway events, mirror.

Real sockets on a real event loop (no mocks): a local TCP echo server
stands in for an allowlisted upstream, and the test speaks the same wire
protocol ``netgw`` speaks.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from scitex_agent_container._sandbox._proxy import (
    ALLOWED_LOG,
    BLOCKED_LOG,
    EgressProxy,
    split_target,
)


@pytest.fixture
def echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            data = conn.recv(1024)
            conn.sendall(b"echo:" + data)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture
def proxy(tmp_path, echo_server):
    p = EgressProxy(
        tmp_path / "sock" / "proxy.sock",
        tmp_path / "out",
        [("localhost", echo_server)],
        ingress_path=tmp_path / "sock" / "ingress.sock",
    )
    p.start()
    yield p
    p.stop()


def _unix(path: Path) -> socket.socket:
    """Connect like netgw does; long tmp paths go through a dir fd (Linux)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    fd = os.open(Path(path).parent, os.O_PATH | os.O_DIRECTORY)
    try:
        s.connect(f"/proc/self/fd/{fd}/{Path(path).name}")
    finally:
        os.close(fd)
    return s


def _connect(proxy, target: str) -> bytes:
    s = _unix(proxy.sock_path)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = s.recv(1024)
    if b" 200 " in head:
        s.sendall(b"ping")
        head += s.recv(1024)
    s.close()
    return head


def test_allowlisted_target_is_tunneled(proxy, echo_server):
    # Arrange
    target = f"localhost:{echo_server}"
    # Act
    got = _connect(proxy, target)
    # Assert
    assert got.endswith(b"echo:ping")


def test_other_target_gets_403(proxy):
    # Arrange
    target = "github.com:443"
    # Act
    got = _connect(proxy, target)
    # Assert
    assert got.startswith(b"HTTP/1.1 403")


def test_refusal_is_logged(proxy):
    # Arrange
    _connect(proxy, "github.com:443")
    # Act
    lines = (proxy.out_dir / BLOCKED_LOG).read_text().splitlines()
    # Assert
    assert json.loads(lines[-1])["host"] == "github.com"


def test_tunnel_is_logged(proxy, echo_server):
    # Arrange
    _connect(proxy, f"localhost:{echo_server}")
    # Act
    lines = (proxy.out_dir / ALLOWED_LOG).read_text().splitlines()
    # Assert
    assert json.loads(lines[-1])["port"] == echo_server


def test_plain_http_is_refused(proxy):
    # Arrange
    s = _unix(proxy.sock_path)
    # Act
    s.sendall(b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
    got = s.recv(1024)
    # Assert
    assert got.startswith(b"HTTP/1.1 403")


def test_gateway_blocked_event_is_logged(proxy):
    # Arrange
    s = _unix(proxy.sock_path)
    ev = {"event": "blocked", "via": "direct", "host": "1.2.3.4", "port": 443}
    # Act
    s.sendall(b"STXLOG\n" + json.dumps(ev).encode() + b"\n")
    s.close()
    time.sleep(0.3)
    # Assert
    assert proxy.summary()["blocked"] == {"1.2.3.4:443": 1}


def test_listen_event_opens_a_host_mirror(proxy, tmp_path):
    # Arrange: the container side of the ingress socket
    got: list[bytes] = []
    ingress = socket.socket(socket.AF_UNIX)
    sock_dir = os.open(tmp_path / "sock", os.O_PATH | os.O_DIRECTORY)
    ingress.bind(f"/proc/self/fd/{sock_dir}/ingress.sock")
    os.close(sock_dir)
    ingress.listen()

    def container_side():
        conn, _ = ingress.accept()
        got.append(conn.recv(64))
        conn.close()

    threading.Thread(target=container_side, daemon=True).start()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    log = _unix(proxy.sock_path)
    log.sendall(
        b"STXLOG\n" + json.dumps({"event": "listen", "port": port}).encode() + b"\n"
    )
    time.sleep(0.3)
    # Act
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    c.sendall(b"hi")
    time.sleep(0.3)
    c.close()
    log.close()
    # Assert
    assert got and got[0].startswith(f"FWD {port}\n".encode())


def test_long_socket_paths_are_served(tmp_path, echo_server):
    # Arrange
    deep = tmp_path.joinpath(*(["d" * 20] * 6))
    p = EgressProxy(deep / "proxy.sock", tmp_path / "out", [("localhost", echo_server)])
    p.start()
    # Act
    try:
        exists = (deep / "proxy.sock").exists()
    finally:
        p.stop()
    # Assert
    assert len(str(deep / "proxy.sock")) > 108 and exists


@pytest.mark.parametrize(
    "target,expected",
    [
        ("pypi.org:443", ("pypi.org", 443)),
        ("[::1]:8080", ("::1", 8080)),
        ("x", ("x", None)),
    ],
)
def test_split_target_parses_host_and_port(target, expected):
    # Arrange
    # Act
    got = split_target(target)
    # Assert
    assert got == expected
