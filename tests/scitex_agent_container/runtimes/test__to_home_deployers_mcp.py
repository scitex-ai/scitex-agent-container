"""Real-file controls for MCP overlay writes and refusal preservation."""

import json
from contextlib import suppress
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._to_home_deployers import _deploy_mcp_merge
from scitex_agent_container.runtimes._to_home_errors import WorkspaceMcpMergeError


@pytest.fixture
def mcp_files(tmp_path):
    src, dst = tmp_path / "agent.json", tmp_path / "home.json"
    src.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"env": {"CHANGE": "agent", "ADD": "new"}},
                    "agent-only": {"command": "agent-command"},
                }
            }
        )
    )
    dst.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {
                        "command": "base-command",
                        "env": {
                            "CHANGE": "baseline",
                            "KEEP": "original",
                        },
                    },
                    "base-only": {"command": "baseline-command"},
                }
            }
        )
    )
    return src, dst


def test_deploy_mcp_merge_preserves_nested_baseline_values(mcp_files):
    # Arrange
    src, dst = mcp_files
    # Act
    _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert json.loads(dst.read_text())["mcpServers"]["shared"] == {
        "command": "base-command",
        "env": {
            "CHANGE": "agent",
            "ADD": "new",
            "KEEP": "original",
        },
    }


def test_deploy_mcp_merge_unions_disjoint_server_names(mcp_files):
    # Arrange
    src, dst = mcp_files
    # Act
    _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert set(json.loads(dst.read_text())["mcpServers"]) == {
        "shared",
        "agent-only",
        "base-only",
    }


def test_deploy_mcp_merge_logs_nested_override_warning(mcp_files, caplog):
    # Arrange
    src, dst = mcp_files
    # Act
    with caplog.at_level("WARNING"):
        _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert "mcpServers['shared'].env.CHANGE: per-agent overrides" in caplog.text


def test_deploy_mcp_merge_preserves_source_file_bytes(mcp_files):
    # Arrange
    src, dst = mcp_files
    original = src.read_bytes()
    # Act
    _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert src.read_bytes() == original


def test_deploy_mcp_merge_refuses_invalid_source_json(mcp_files):
    # Arrange
    src, dst = mcp_files
    src.write_text("invalid-source{")
    # Act
    refusal = pytest.raises(WorkspaceMcpMergeError, match="source .*not valid JSON")
    # Assert
    with refusal:
        _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))


def test_deploy_mcp_merge_preserves_destination_on_invalid_source(mcp_files):
    # Arrange
    src, dst = mcp_files
    src.write_text("invalid-source{")
    original = dst.read_bytes()
    # Act
    with suppress(WorkspaceMcpMergeError):
        _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert dst.read_bytes() == original


def test_deploy_mcp_merge_refuses_invalid_destination_json(mcp_files):
    # Arrange
    src, dst = mcp_files
    dst.write_text("invalid-destination{")
    # Act
    refusal = pytest.raises(WorkspaceMcpMergeError, match="existing .*not valid JSON")
    # Assert
    with refusal:
        _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))


def test_deploy_mcp_merge_preserves_invalid_destination_bytes(mcp_files):
    # Arrange
    src, dst = mcp_files
    dst.write_text("invalid-destination{")
    original = dst.read_bytes()
    # Act
    with suppress(WorkspaceMcpMergeError):
        _deploy_mcp_merge(src, dst, config=None, rel=Path(".mcp.json"))
    # Assert
    assert dst.read_bytes() == original
