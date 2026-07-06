"""Tests for the fleet MCP cold-start reliability knobs.

Two deterministic, config-only levers distributed via to_home:
  * ``inject_always_load`` stamps ``alwaysLoad:true`` onto the critical stdio
    MCP servers so Claude Code BLOCKS on startup until they connect.
  * ``mcp_timeout_env_flags`` raises the client MCP startup connect timeout.

Conventions: one assert / AAA markers; no mocks — pure dict / list inputs.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._mcp_reliability import (
    CRITICAL_MCP_SERVERS,
    MCP_STARTUP_TIMEOUT_MS,
    inject_always_load,
    mcp_timeout_env_flags,
)


def test_inject_always_load_stamps_sac_server():
    # Arrange
    doc = {"mcpServers": {"scitex-agent-container": {"command": "sac"}}}
    # Act
    inject_always_load(doc)
    # Assert
    assert doc["mcpServers"]["scitex-agent-container"]["alwaysLoad"] is True


def test_inject_always_load_stamps_todo_server():
    # Arrange
    doc = {"mcpServers": {"scitex-todo": {"command": "scitex-todo"}}}
    # Act
    inject_always_load(doc)
    # Assert
    assert doc["mcpServers"]["scitex-todo"]["alwaysLoad"] is True


def test_inject_always_load_leaves_non_critical_servers_untouched():
    # Arrange
    doc = {"mcpServers": {"claude-code-telegrammer": {"command": "bun"}}}
    # Act
    inject_always_load(doc)
    # Assert — a non-critical server must NOT be forced to block startup.
    assert "alwaysLoad" not in doc["mcpServers"]["claude-code-telegrammer"]


def test_inject_always_load_respects_explicit_override():
    # Arrange — a deliberate per-agent opt-out must survive.
    doc = {"mcpServers": {"scitex-todo": {"command": "x", "alwaysLoad": False}}}
    # Act
    inject_always_load(doc)
    # Assert
    assert doc["mcpServers"]["scitex-todo"]["alwaysLoad"] is False


def test_inject_always_load_skips_absent_server():
    # Arrange — only one of the two critical servers present.
    doc = {"mcpServers": {"scitex-agent-container": {"command": "sac"}}}
    # Act
    inject_always_load(doc)
    # Assert
    assert "scitex-todo" not in doc["mcpServers"]


def test_inject_always_load_fail_open_on_missing_mcpservers_key():
    # Arrange — no mcpServers key at all.
    doc = {"unrelated": 1}
    # Act
    out = inject_always_load(doc)
    # Assert — returned unchanged, no raise.
    assert out == {"unrelated": 1}


def test_inject_always_load_fail_open_on_non_dict_mcpservers():
    # Arrange — malformed mcpServers value.
    doc = {"mcpServers": []}
    # Act
    out = inject_always_load(doc)
    # Assert
    assert out == {"mcpServers": []}


def test_inject_always_load_is_idempotent():
    # Arrange
    doc = {"mcpServers": {"scitex-todo": {"command": "x"}}}
    # Act
    inject_always_load(doc)
    inject_always_load(doc)
    # Assert
    assert doc["mcpServers"]["scitex-todo"]["alwaysLoad"] is True


def test_mcp_timeout_env_flags_shape():
    # Arrange
    expected = ["--env", f"MCP_TIMEOUT={MCP_STARTUP_TIMEOUT_MS}"]
    # Act
    flags = mcp_timeout_env_flags()
    # Assert — apptainer `--env KEY=VALUE` pair.
    assert flags == expected


def test_mcp_timeout_is_generous():
    # Arrange
    minimum_ms = 30000
    # Act
    value = int(MCP_STARTUP_TIMEOUT_MS)
    # Assert — must cover the multi-second fastmcp cold-start.
    assert value >= minimum_ms


def test_critical_servers_include_sac():
    # Arrange
    server = "scitex-agent-container"
    # Act
    present = server in CRITICAL_MCP_SERVERS
    # Assert
    assert present is True


def test_critical_servers_include_todo():
    # Arrange
    server = "scitex-todo"
    # Act
    present = server in CRITICAL_MCP_SERVERS
    # Assert
    assert present is True
