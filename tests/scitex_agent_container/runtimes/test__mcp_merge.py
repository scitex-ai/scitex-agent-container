"""Tests for the `.mcp.json` deep-merge with per-agent precedence.

The shared baseline `_shared/to_home/.mcp.json` must DEEP-MERGE with each
agent's own `.mcp.json` — union the `mcpServers` so every agent inherits the
default servers (sac / scitex-todo / claude-code-telegrammer) AND keeps its
own. A plain full-overwrite would silently drop the baseline defaults.

Contract (operator 2026-07-02 — per-agent precedence, warn-not-fatal):
  * disjoint server names  → union.
  * same name, IDENTICAL definition → kept once (idempotent).
  * same name, DIFFERENT definition → recursively DEEP-MERGE with the per-agent
    (overlay) value winning on leaf conflicts; a genuine override is LOGGED at
    WARNING (visible, not fatal).

Conventions: one assert / AAA markers; no mocks — pure dict inputs (`caplog`
is a real log capture, not a mock).
"""

from __future__ import annotations

from scitex_agent_container.runtimes._mcp_merge import merge_mcp_json


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


def test_conflicting_same_name_server_per_agent_wins():
    # Arrange — same name, different command → per-agent (overlay) wins.
    base = {"mcpServers": {"sac": {"command": "/opt/venv-sac/bin/sac"}}}
    overlay = {"mcpServers": {"sac": {"command": "/usr/bin/sac"}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["mcpServers"]["sac"]["command"] == "/usr/bin/sac"


def test_same_name_server_env_deep_merges_per_agent_wins():
    # Arrange — overlay overrides one env key; baseline's other env key survives.
    base = {"mcpServers": {"cct": {"env": {"CCT_AGENT_ID": "sac", "KEEP": "yes"}}}}
    overlay = {"mcpServers": {"cct": {"env": {"CCT_AGENT_ID": "neurovista"}}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["mcpServers"]["cct"]["env"] == {
        "CCT_AGENT_ID": "neurovista",
        "KEEP": "yes",
    }


def test_conflicting_same_name_server_logs_warning(caplog):
    # Arrange — a genuine override should be visible (WARNING), not silent.
    base = {"mcpServers": {"sac": {"command": "/opt/venv-sac/bin/sac"}}}
    overlay = {"mcpServers": {"sac": {"command": "/usr/bin/sac"}}}
    # Act
    with caplog.at_level("WARNING"):
        merge_mcp_json(base, overlay)
    # Assert
    assert "per-agent overrides shared baseline" in caplog.text


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


def test_baseline_always_load_survives_overlay_without_it():
    # Arrange — the cold-start-race fix stamps ``alwaysLoad`` on the baseline
    # server; an agent overlay that overrides only a leaf (env) must NOT drop
    # it (fleet incident 2026-07-06 — the merge preserves unknown per-server
    # fields).
    base = {"mcpServers": {"scitex-todo": {"command": "st", "alwaysLoad": True}}}
    overlay = {"mcpServers": {"scitex-todo": {"env": {"X": "1"}}}}
    # Act
    merged = merge_mcp_json(base, overlay)
    # Assert
    assert merged["mcpServers"]["scitex-todo"]["alwaysLoad"] is True
