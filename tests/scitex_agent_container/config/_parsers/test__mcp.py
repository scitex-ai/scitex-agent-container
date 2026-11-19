"""``spec.mcp_servers`` load pass: interpolation + ``inherit_env`` validation."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._mcp import interpolate_mcp_servers


def test_valid_inherit_env_list_survives_the_load_pass():
    # Arrange
    raw = {"review": {"command": "x", "inherit_env": ["PGUSER"]}}
    # Act
    result = interpolate_mcp_servers(raw, {"name": "a"})
    # Assert
    assert result["review"]["inherit_env"] == ["PGUSER"]


def test_inherit_env_string_is_refused_at_load():
    # Arrange
    raw = {"review": {"command": "x", "inherit_env": "false"}}
    # Act
    ctx = pytest.raises(ValueError, match=r"spec\.mcp_servers\.review\.inherit_env")
    # Assert
    with ctx:
        interpolate_mcp_servers(raw, {"name": "a"})


def test_metadata_name_is_interpolated_into_env():
    # Arrange
    raw = {"s": {"command": "x", "env": {"ID": "${metadata.name}"}}}
    # Act
    result = interpolate_mcp_servers(raw, {"name": "agent-a"})
    # Assert
    assert result["s"]["env"] == {"ID": "agent-a"}
