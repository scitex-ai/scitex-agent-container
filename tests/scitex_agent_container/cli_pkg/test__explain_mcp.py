"""``sac agents explain``: per-MCP-entry env NAMES and inherit_env mode."""

from __future__ import annotations

from scitex_agent_container.cli_pkg._explain_mcp import mcp_env_lines
from scitex_agent_container.config import AgentConfig

_SECRET_DSN = "postgresql://fixture.invalid:65432/secret_db_name"


def _config() -> AgentConfig:
    return AgentConfig(
        name="reader",
        env={"SCITEX_STORE_DSN": _SECRET_DSN, "PGUSER": "fixture__reader"},
        mcp_servers={
            "cards": {"command": "scitex-cards"},
            "review": {
                "command": "scitex-live-paper",
                "env": {"REPORT": "/work/r.json"},
                "inherit_env": False,
            },
            "partial": {"command": "x", "inherit_env": ["PGUSER"]},
            "remote": {"type": "http", "url": "http://example.invalid/mcp"},
        },
    )


def test_store_free_entry_lists_only_declared_names():
    # Arrange
    config = _config()
    # Act
    lines = mcp_env_lines(config)
    # Assert
    assert "  review: inherit_env=false — env: REPORT" in lines


def test_default_entry_lists_inherited_spec_env_names():
    # Arrange
    config = _config()
    # Act
    lines = mcp_env_lines(config)
    # Assert
    assert "  cards: inherit_env=true — env: PGUSER, SCITEX_STORE_DSN" in lines


def test_allowlist_entry_lists_only_allowed_names():
    # Arrange
    config = _config()
    # Act
    lines = mcp_env_lines(config)
    # Assert
    assert "  partial: inherit_env=[PGUSER] — env: PGUSER" in lines


def test_remote_entry_is_marked_without_child_env():
    # Arrange
    config = _config()
    # Act
    lines = mcp_env_lines(config)
    # Assert
    assert "  remote: inherit_env=true — remote transport, no child env" in lines


def test_explain_mcp_lines_never_show_env_values():
    # Arrange
    config = _config()
    # Act
    text = "\n".join(mcp_env_lines(config))
    # Assert
    assert "secret_db_name" not in text


def test_agent_without_mcp_servers_renders_nothing():
    # Arrange
    config = AgentConfig(name="bare")
    # Act
    lines = mcp_env_lines(config)
    # Assert
    assert lines == []
