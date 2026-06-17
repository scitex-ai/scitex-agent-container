"""Tests for the fail-loud `.mcp.json` deep-merge (W1 / operator 2026-06-17).

The shared baseline `_shared/to_home/.mcp.json` must DEEP-MERGE with each
agent's own `.mcp.json` — union the `mcpServers` so every agent inherits the
default servers (sac / scitex-todo / claude-code-telegrammer) AND keeps its
own. Today the to_home deploy FULL-OVERWRITES `.mcp.json`, so a per-agent file
would silently drop the baseline defaults — exactly the silent-fallback the
operator forbids.

Contract (fail-fast, fail-loud, no silent fallback):
  * disjoint server names  → union.
  * same name, IDENTICAL definition → kept once (idempotent).
  * same name, DIFFERENT definition → raise `McpMergeConflict` (the operator
    must resolve it explicitly; never silently pick a winner).

Conventions: one assert / AAA markers (a `pytest.raises` block IS the assert);
no mocks — pure dict inputs.
"""

from __future__ import annotations

import pytest
from scitex_agent_container.runtimes._mcp_merge import (
    McpMergeConflict,
    merge_mcp_json,
)


def test_disjoint_servers_are_unioned():
    # Arrange
    base = {"mcpServers": {"sac": {"command": "sac"}}}
    overlay = {"mcpServers": {"todo": {"command": "todo"}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert set(merged["mcpServers"]) == {"sac", "todo"}


def test_identical_same_name_server_is_kept_once():
    # Arrange
    srv = {"command": "sac", "args": ["mcp", "start"]}
    base = {"mcpServers": {"sac": srv}}
    overlay = {"mcpServers": {"sac": dict(srv)}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["mcpServers"]["sac"] == srv


def test_conflicting_same_name_server_fails_loud():
    # Arrange — same name, different command → must NOT silently pick one.
    base = {"mcpServers": {"sac": {"command": "/opt/venv-sac/bin/sac"}}}
    overlay = {"mcpServers": {"sac": {"command": "/usr/bin/sac"}}}
    # Act
    # Assert
    with pytest.raises(McpMergeConflict):
        merge_mcp_json(base, overlay)


def test_overlay_only_servers_survive_when_baseline_empty():
    # Arrange
    base = {"mcpServers": {}}
    overlay = {"mcpServers": {"todo": {"command": "todo"}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["mcpServers"] == {"todo": {"command": "todo"}}


def test_baseline_defaults_survive_an_agents_own_extra_server():
    # Arrange — the core W1 guarantee: agent's extra server PLUS the defaults.
    base = {"mcpServers": {"sac": {"command": "sac"}, "todo": {"command": "todo"}}}
    overlay = {"mcpServers": {"figrecipe": {"command": "fr"}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert set(merged["mcpServers"]) == {"sac", "todo", "figrecipe"}


def test_non_mcpservers_keys_are_preserved_from_both():
    # Arrange — top-level keys other than mcpServers (e.g. a comment) merge too.
    base = {"mcpServers": {"sac": {"command": "sac"}}, "_note": "baseline"}
    overlay = {"mcpServers": {"todo": {"command": "todo"}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["_note"] == "baseline"
