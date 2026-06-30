"""Tests for the per-agent telegrammer ``CCT_STATE_DIR`` override.

Incident (2026-07-01): every agent wired for the ``claude-code-telegrammer``
stdio MCP shipped the SAME hardcoded ``CCT_STATE_DIR``
(``/home/agent/.claude-code-telegrammer-dev``). The telegrammer keys its
pidfile + messages.db on that dir, so all agents collided on one state dir and
its newest-wins takeover left only ONE agent connected. The fix rewrites the
state dir to a DISTINCT per-agent value at ``.mcp.json`` materialization.

PA-306 no-mocks: drive the real ``deploy_to_home`` entrypoint against
``tmp_path`` real directories with real ``AgentConfig`` instances; assert on
the materialized ``.mcp.json``. STX-TQ007: ONE logical assert per test.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._to_home import deploy_to_home

# A per-agent ``.mcp.json`` carrying the telegrammer server with the buggy
# shared ``-dev`` state dir and the runtime-expanded $VAR env that must be
# left UNTOUCHED.
_TELEGRAMMER_MCP = {
    "mcpServers": {
        "claude-code-telegrammer": {
            "command": "claude-code-telegrammer",
            "args": ["mcp"],
            "env": {
                "CCT_STATE_DIR": "/home/agent/.claude-code-telegrammer-dev",
                "CCT_BOT_TOKEN": "$CCT_BOT_TOKEN",
                "CCT_AGENT_ID": "$CCT_AGENT_ID",
            },
        }
    }
}


def _deploy_with_mcp(tmp_path: Path, agent_name: str, mcp_doc: dict) -> Path:
    """Run a real ``deploy_to_home`` for ``agent_name`` with ``mcp_doc`` as the
    per-agent ``to_home/.mcp.json``. Returns the materialized ``.mcp.json``."""
    agent_dir = tmp_path / agent_name
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True, exist_ok=True)
    (to_home / ".mcp.json").write_text(json.dumps(mcp_doc) + "\n")
    cfg = AgentConfig(name=agent_name)
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.to_home = ""
    home = tmp_path / "home"
    deploy_to_home(cfg, str(home))
    return home / ".mcp.json"


def _state_dir(mcp_path: Path) -> str:
    doc = json.loads(mcp_path.read_text())
    server = doc["mcpServers"]["claude-code-telegrammer"]
    return server["env"]["CCT_STATE_DIR"]


class TestPerAgentTelegrammerStateDir:
    def test_state_dir_is_per_agent(self, tmp_path):
        # Arrange — an agent whose .mcp.json has the telegrammer server.
        mcp_path = _deploy_with_mcp(tmp_path, "alpha", _TELEGRAMMER_MCP)
        # Act
        # Assert — CCT_STATE_DIR is rewritten to the per-agent value.
        assert _state_dir(mcp_path) == "/home/agent/.claude-code-telegrammer-alpha"

    def test_two_agents_get_distinct_state_dirs(self, tmp_path):
        # Arrange — two different agent names (the anti-collision property).
        a_path = _deploy_with_mcp(tmp_path / "a", "alpha", _TELEGRAMMER_MCP)
        b_path = _deploy_with_mcp(tmp_path / "b", "bravo", _TELEGRAMMER_MCP)
        # Act
        # Assert — distinct names ⇒ distinct state dirs ⇒ no collision.
        assert _state_dir(a_path) != _state_dir(b_path)

    def test_bot_token_env_untouched(self, tmp_path):
        # Arrange — only CCT_STATE_DIR is touched; $VAR env stays literal.
        mcp_path = _deploy_with_mcp(tmp_path, "alpha", _TELEGRAMMER_MCP)
        # Act
        doc = json.loads(mcp_path.read_text())
        env = doc["mcpServers"]["claude-code-telegrammer"]["env"]
        # Assert
        assert env["CCT_BOT_TOKEN"] == "$CCT_BOT_TOKEN"

    def test_non_telegrammer_agent_unchanged(self, tmp_path):
        # Arrange — an agent with NO telegrammer server: no state dir fabricated.
        other = {"mcpServers": {"sac": {"command": "sac", "args": ["mcp"]}}}
        mcp_path = _deploy_with_mcp(tmp_path, "alpha", other)
        # Act
        doc = json.loads(mcp_path.read_text())
        # Assert — strict no-op: the doc carries no telegrammer/CCT_STATE_DIR.
        assert "claude-code-telegrammer" not in doc["mcpServers"]
