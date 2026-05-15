"""Tests for config._parsers._listen.parse_listen."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._listen import parse_listen


def test_missing_listen_key_returns_empty_list():
    # Arrange
    spec: dict = {}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_non_list_listen_value_returns_empty_list():
    # Arrange
    spec = {"listen": "tcp:8080"}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_explicit_none_listen_value_returns_empty_list():
    # Arrange
    spec = {"listen": None}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_tcp_with_valid_port_yields_single_entry():
    # Arrange
    spec = {"listen": [{"proto": "tcp", "port": 8080, "name": "api"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert len(result) == 1


def test_tcp_with_valid_port_preserves_proto_field():
    # Arrange
    spec = {"listen": [{"proto": "tcp", "port": 8080, "name": "api"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].proto == "tcp"


def test_tcp_with_valid_port_preserves_port_field():
    # Arrange
    spec = {"listen": [{"proto": "tcp", "port": 8080, "name": "api"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].port == 8080


def test_tcp_with_valid_port_preserves_name_field():
    # Arrange
    spec = {"listen": [{"proto": "tcp", "port": 8080, "name": "api"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].name == "api"


@pytest.mark.parametrize(
    "entry",
    [
        {"proto": "tcp", "port": 0},
        {"proto": "tcp", "port": "abc"},
        {"proto": "udp", "port": -1},
    ],
    ids=["tcp-zero-port", "tcp-non-numeric-port", "udp-negative-port"],
)
def test_tcp_or_udp_with_invalid_port_is_dropped(entry):
    # Arrange
    spec = {"listen": [entry]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_unix_socket_with_path_yields_single_entry():
    # Arrange
    spec = {"listen": [{"proto": "unix", "path": "/run/sac.sock", "owner": "sac"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert len(result) == 1


def test_unix_socket_preserves_proto_field():
    # Arrange
    spec = {"listen": [{"proto": "unix", "path": "/run/sac.sock", "owner": "sac"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].proto == "unix"


def test_unix_socket_preserves_path_field():
    # Arrange
    spec = {"listen": [{"proto": "unix", "path": "/run/sac.sock", "owner": "sac"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].path == "/run/sac.sock"


def test_unix_socket_preserves_owner_field():
    # Arrange
    spec = {"listen": [{"proto": "unix", "path": "/run/sac.sock", "owner": "sac"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].owner == "sac"


def test_unix_socket_without_path_is_dropped():
    # Arrange
    spec = {"listen": [{"proto": "unix"}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_non_dict_entries_are_filtered_out():
    # Arrange
    spec = {"listen": ["not-a-dict", None, {"proto": "tcp", "port": 1}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert len(result) == 1


def test_default_proto_with_missing_port_is_dropped():
    # Arrange
    spec = {"listen": [{}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result == []


def test_default_proto_is_tcp_when_omitted():
    # Arrange
    spec = {"listen": [{"port": 22}]}
    # Act
    result = parse_listen(spec)
    # Assert
    assert result[0].proto == "tcp"
