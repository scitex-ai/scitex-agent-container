"""The telegrammer MCP must be gated on the spec, not on the env.

Incident 2026-07-17: the shared baseline .mcp.json carries a
claude-code-telegrammer MCP for EVERY agent. spec.claude.channels gated only the
--dangerously-load-development-channels flag, never the server. Once direnv began
loading each project's .envrc in the agent's shell, three scitex-cards UI agents
whose cwd was ~/proj/scitex-todo picked up that project's CCT_BOT_TOKEN *and*
CCT_AGENT_ID, came up announcing themselves as scitex-todo, and fought over the
scitex-todo steward's bot.

These are AAA and each name states the behaviour, not the implementation.
"""

import json
import types
from pathlib import Path

from scitex_agent_container.runtimes._to_home_deployers import _deploy_mcp_merge

_BASELINE = {
    "mcpServers": {
        "sac": {"command": "sac"},
        "scitex-todo": {"command": "scitex-todo"},
        "claude-code-telegrammer": {
            "command": "cct",
            "env": {"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"},
        },
    }
}


def _config(name, channels):
    """A stand-in AgentConfig: only .name/.workdir/.claude.channels are read."""
    return types.SimpleNamespace(
        name=name,
        workdir="/home/ywatanabe/proj/scitex-todo",
        claude=types.SimpleNamespace(channels=list(channels)),
        env={},
    )


def _run(tmp_path, config):
    """Deploy the baseline, then merge a per-agent overlay over it."""
    src = tmp_path / "src.mcp.json"
    src.write_text(json.dumps(_BASELINE))
    dst = tmp_path / ".mcp.json"
    _deploy_mcp_merge(src, dst, config=config, rel=Path(".mcp.json"))
    return json.loads(dst.read_text()).get("mcpServers", {})


def test_agent_whose_spec_omits_the_channel_gets_no_telegrammer_mcp(tmp_path):
    # Arrange: the exact shape of the three scitex-cards UI agents.
    config = _config("scitex-cards-chat", ["server:sac", "server:scitex-todo"])
    # Act
    servers = _run(tmp_path, config)
    # Assert: the server that would have polled a stolen bot is simply absent.
    assert "claude-code-telegrammer" not in servers


def test_agent_whose_spec_requests_the_channel_keeps_its_telegrammer_mcp(tmp_path):
    # Arrange
    config = _config("dotfiles", ["server:sac", "server:claude-code-telegrammer"])
    # Act
    servers = _run(tmp_path, config)
    # Assert: the gate must not break the agents that legitimately have a bot.
    assert "claude-code-telegrammer" in servers


def test_gating_the_telegrammer_leaves_every_other_baseline_server_intact(tmp_path):
    # Arrange
    config = _config("scitex-cards-gui", ["server:sac"])
    # Act
    servers = _run(tmp_path, config)
    # Assert: a scalpel, not a hammer -- only the one server is dropped.
    assert "sac" in servers and "scitex-todo" in servers


def test_baseline_pass_without_a_config_keeps_the_telegrammer_untouched(tmp_path):
    # Arrange: the config-less baseline deploy has no spec to consult; stripping
    # there would be a fallback, and the agent's own pass always follows.
    # Act
    servers = _run(tmp_path, None)
    # Assert
    assert "claude-code-telegrammer" in servers
