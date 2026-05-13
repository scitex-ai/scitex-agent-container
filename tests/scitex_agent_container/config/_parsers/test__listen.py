"""Tests for config._parsers._listen.parse_listen."""

from __future__ import annotations

from scitex_agent_container.config._parsers._listen import parse_listen


def test_missing_returns_empty():
    assert parse_listen({}) == []


def test_non_list_returns_empty():
    assert parse_listen({"listen": "tcp:8080"}) == []


def test_explicit_none_returns_empty():
    assert parse_listen({"listen": None}) == []


def test_tcp_with_port_kept():
    out = parse_listen({"listen": [{"proto": "tcp", "port": 8080, "name": "api"}]})
    assert len(out) == 1
    p = out[0]
    assert p.proto == "tcp"
    assert p.port == 8080
    assert p.name == "api"


def test_tcp_with_zero_port_dropped():
    assert parse_listen({"listen": [{"proto": "tcp", "port": 0}]}) == []


def test_tcp_with_invalid_port_dropped():
    assert parse_listen({"listen": [{"proto": "tcp", "port": "abc"}]}) == []


def test_udp_with_negative_port_dropped():
    assert parse_listen({"listen": [{"proto": "udp", "port": -1}]}) == []


def test_unix_socket_with_path_kept():
    out = parse_listen(
        {"listen": [{"proto": "unix", "path": "/run/sac.sock", "owner": "sac"}]}
    )
    assert len(out) == 1
    assert out[0].proto == "unix"
    assert out[0].path == "/run/sac.sock"
    assert out[0].owner == "sac"


def test_unix_socket_without_path_dropped():
    assert parse_listen({"listen": [{"proto": "unix"}]}) == []


def test_non_dict_entries_dropped():
    out = parse_listen({"listen": ["not-a-dict", None, {"proto": "tcp", "port": 1}]})
    assert len(out) == 1


def test_default_proto_is_tcp_requires_port():
    # Default proto="tcp", missing port → dropped
    assert parse_listen({"listen": [{}]}) == []
    out = parse_listen({"listen": [{"port": 22}]})
    assert out[0].proto == "tcp"
