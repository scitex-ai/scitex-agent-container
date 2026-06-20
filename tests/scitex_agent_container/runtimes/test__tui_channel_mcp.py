"""``_tui_channel_mcp`` — register spec.claude.channels backing MCPs into
``$HOME/.claude.json`` so the in-TUI ``claude`` resolves each channel.

Closes the SDK↔TUI channel drift (handoff item 3): a ``channels:
[server:sac]`` spec booted into the TUI showing ``server:sac · no MCP
server configured with that name`` because the inline ``--mcp-config`` sac
subscriber is NOT in a scope claude's channel resolver scans. The fix writes
the backing MCP into the top-level ``mcpServers`` (claude's ``user`` scope),
which the resolver DOES read (verified against the SDK-bundled claude v2.1.150
binary).

Real ``AgentConfig`` via ``load_config`` on a tmp spec + real ``tmp_path``
files — no mocks. STX-TQ002 AAA-marker + STX-TQ007 one-assert + PA-306
no-mock-fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._tui_channel_mcp import (
    ChannelMcpMissingError,
    build_channel_mcp_servers,
    ensure_tui_channel_mcp,
    register_channels_into_claude_json,
)

# ---------------------------------------------------------------------------
# Spec helpers — real AgentConfig from a tmp spec.yaml (no mocks).
# ---------------------------------------------------------------------------

_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  workdir: /tmp/agt-work
  claude:
    model: claude-opus-4-8[1m]
{channels}
"""


def _write_config(tmp_path: Path, channels_block: str):
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(_SPEC.format(channels=channels_block), encoding="utf-8")
    return load_config(str(spec))


_SAC_CHANNEL_BLOCK = """\
    channels:
      - server:sac
"""

_TELEGRAMMER_CHANNEL_BLOCK = """\
    channels:
      - server:claude-code-telegrammer
"""

_BOTH_CHANNELS_BLOCK = """\
    channels:
      - server:sac
      - server:claude-code-telegrammer
"""


def _write_home_mcp(home_dir: Path, servers: dict) -> None:
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8"
    )


def _read_claude_json(home_dir: Path) -> dict:
    return json.loads((home_dir / ".claude.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# build_channel_mcp_servers
# ---------------------------------------------------------------------------


def test_build_sac_channel_keys_the_bare_mcp_name(tmp_path) -> None:
    # Arrange
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    servers = build_channel_mcp_servers(config, home_dir)
    # Assert — keyed ``sac`` (the ``server:`` prefix stripped), the name
    # claude resolves the ``server:sac`` channel against.
    assert set(servers) == {"sac"}


def test_build_sac_channel_entry_runs_the_mcp_channel_subscriber(tmp_path) -> None:
    # Arrange
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    servers = build_channel_mcp_servers(config, home_dir)
    # Assert — the synthesised entry execs ``sac mcp channel`` (the bus
    # subscriber), not ``sac mcp start`` (the tools server).
    assert "mcp" in servers["sac"]["args"] and "channel" in servers["sac"]["args"]


def test_build_non_sac_channel_copies_backing_entry_from_home_mcp(tmp_path) -> None:
    # Arrange — the telegrammer backing MCP lives in $HOME/.mcp.json.
    config = _write_config(tmp_path, _TELEGRAMMER_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    backing = {"type": "stdio", "command": "bun", "args": ["run", "tg.ts"]}
    _write_home_mcp(home_dir, {"claude-code-telegrammer": backing})
    # Act
    servers = build_channel_mcp_servers(config, home_dir)
    # Assert
    assert servers["claude-code-telegrammer"] == backing


def test_build_fails_loud_when_non_sac_channel_has_no_backing_mcp(tmp_path) -> None:
    # Arrange — telegrammer channel declared but no backing entry anywhere.
    config = _write_config(tmp_path, _TELEGRAMMER_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    # (the call is under the raises context below)
    # Assert — a declared channel with no resolvable MCP must raise, never
    # silently drop (the warning class this fix removes).
    with pytest.raises(ChannelMcpMissingError):
        build_channel_mcp_servers(config, home_dir)


def test_build_no_channels_returns_empty(tmp_path) -> None:
    # Arrange — no channels block at all.
    config = _write_config(tmp_path, "")
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    servers = build_channel_mcp_servers(config, home_dir)
    # Assert
    assert servers == {}


# ---------------------------------------------------------------------------
# register_channels_into_claude_json
# ---------------------------------------------------------------------------


def test_register_writes_sac_into_user_scope_mcp_servers(tmp_path) -> None:
    # Arrange
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    register_channels_into_claude_json(config, home_dir)
    # Assert — the channel resolver reads the TOP-LEVEL mcpServers (user
    # scope); the sac subscriber must land there keyed ``sac``.
    assert "sac" in _read_claude_json(home_dir)["mcpServers"]


def test_register_marks_the_channel_mcp_enabled(tmp_path) -> None:
    # Arrange
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    register_channels_into_claude_json(config, home_dir)
    # Assert — enabledMcpjsonServers lists the name so the TUI does not
    # prompt to enable it.
    assert "sac" in _read_claude_json(home_dir)["enabledMcpjsonServers"]


def test_register_preserves_existing_claude_json_keys(tmp_path) -> None:
    # Arrange — a pre-seeded .claude.json (as ensure_project_onboarding writes).
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    (home_dir / ".claude.json").write_text(
        json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}) + "\n",
        encoding="utf-8",
    )
    # Act
    register_channels_into_claude_json(config, home_dir)
    # Assert — the onboarding seed is not clobbered.
    assert _read_claude_json(home_dir)["hasCompletedOnboarding"] is True


def test_register_does_not_clobber_an_existing_same_name_entry(tmp_path) -> None:
    # Arrange — operator already configured a ``sac`` MCP by hand.
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    operator_entry = {"type": "stdio", "command": "/custom/sac", "args": ["x"]}
    (home_dir / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sac": operator_entry}}) + "\n",
        encoding="utf-8",
    )
    # Act
    register_channels_into_claude_json(config, home_dir)
    # Assert — operator config wins on name collision.
    assert _read_claude_json(home_dir)["mcpServers"]["sac"] == operator_entry


def test_register_returns_false_when_no_channels(tmp_path) -> None:
    # Arrange
    config = _write_config(tmp_path, "")
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    # Act
    wrote = register_channels_into_claude_json(config, home_dir)
    # Assert
    assert wrote is False


def test_register_both_channels_registers_both_names(tmp_path) -> None:
    # Arrange — sac (synthesised) + telegrammer (from $HOME/.mcp.json).
    config = _write_config(tmp_path, _BOTH_CHANNELS_BLOCK)
    home_dir = tmp_path / "home"
    backing = {"type": "stdio", "command": "bun", "args": ["run", "tg.ts"]}
    _write_home_mcp(home_dir, {"claude-code-telegrammer": backing})
    # Act
    register_channels_into_claude_json(config, home_dir)
    # Assert
    assert {"sac", "claude-code-telegrammer"} <= set(
        _read_claude_json(home_dir)["mcpServers"]
    )


# ---------------------------------------------------------------------------
# ensure_tui_channel_mcp — the materialize_workspace entrypoint
# ---------------------------------------------------------------------------


def test_ensure_writes_into_every_provided_home(tmp_path) -> None:
    # Arrange — two homes (workspace-home + overlay-upper-home).
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    # Act
    ensure_tui_channel_mcp(config, home_a, home_b)
    # Assert — both homes get the registration so it lands regardless of the
    # spec's home-delivery mode.
    assert "sac" in _read_claude_json(home_a)["mcpServers"] and (
        "sac" in _read_claude_json(home_b)["mcpServers"]
    )


def test_ensure_skips_none_and_missing_homes(tmp_path) -> None:
    # Arrange — a real home plus a None (no overlay) and a non-existent dir.
    config = _write_config(tmp_path, _SAC_CHANNEL_BLOCK)
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "nope"
    # Act — None / missing dirs are skipped, the real one is written.
    ensure_tui_channel_mcp(config, home, None, missing)
    # Assert
    assert (home / ".claude.json").is_file() and not missing.exists()


def test_ensure_propagates_fail_loud_for_missing_backing_mcp(tmp_path) -> None:
    # Arrange — telegrammer channel with no backing MCP in the home.
    config = _write_config(tmp_path, _TELEGRAMMER_CHANNEL_BLOCK)
    home = tmp_path / "home"
    home.mkdir()
    # Act
    # (the call is under the raises context below)
    # Assert — the error must stop the start, not boot a mute agent.
    with pytest.raises(ChannelMcpMissingError):
        ensure_tui_channel_mcp(config, home)
