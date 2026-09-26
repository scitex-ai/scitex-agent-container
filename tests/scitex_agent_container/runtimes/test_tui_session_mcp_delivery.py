"""TUI workspace delivery for explicit ``spec.mcp_servers``.

The TUI binds SAC's isolated ``<state>/home`` as the container ``$HOME``.
Consequently, an MCP declaration written only to the project workdir is
invisible to both Claude's interactive TUI and Hermes' profile translator.
These tests exercise the materialized file that the runtime actually consumes.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_profile, _pg_identity_credentials
from scitex_agent_container.runtimes._tui_workspace import materialize_workspace


@contextmanager
def _isolated_home(root: Path) -> Iterator[None]:
    baseline = root / "baseline"
    baseline.mkdir()
    replacements = {
        "HOME": str(root / "host-home"),
        "SAC_TO_HOME_BASELINE": str(baseline),
    }
    previous = {key: os.environ.get(key) for key in replacements}
    os.environ.update(replacements)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _replace_attributes(replacements: list[tuple[Any, str, Any]]) -> Iterator[None]:
    originals = [
        (target, name, getattr(target, name)) for target, name, _value in replacements
    ]
    try:
        for target, name, value in replacements:
            setattr(target, name, value)
        yield
    finally:
        for target, name, value in originals:
            setattr(target, name, value)


def test_tui_workspace_materializes_explicit_mcp_server(tmp_path: Path) -> None:
    # Arrange
    config = AgentConfig(
        name="hermes-cct-canary",
        runtime="tui",
        harness="hermes",
        workdir=str(tmp_path / "work"),
        env={"PGUSER": "project-role"},
        labels={},
        mcp_servers={
            "claude-code-telegrammer": {
                "type": "stdio",
                "command": "/opt/cct-bun/bin/bun",
                "args": ["run", "/opt/cct/telegram-server.ts"],
                "alwaysLoad": True,
                "env": {
                    "CCT_BOT_TOKEN": "",
                    "CCT_AGENT_ID": "hermes-cct-canary",
                },
            }
        },
    )
    state_dir = tmp_path / "state"

    # Act
    with _isolated_home(tmp_path):
        home = materialize_workspace(
            config, state_dir_for_config=lambda _config: state_dir
        )
    mcp_path = home / ".mcp.json"
    document = json.loads(mcp_path.read_text(encoding="utf-8"))
    server = document["mcpServers"]["claude-code-telegrammer"]
    observed = {
        "mode": stat.S_IMODE(mcp_path.stat().st_mode),
        "command": server["command"],
        "always_load": server["alwaysLoad"],
        "agent_id": server["env"]["CCT_AGENT_ID"],
        "bot_token": server["env"]["CCT_BOT_TOKEN"],
        "pguser": server["env"]["PGUSER"],
    }

    # Assert
    assert observed == {
        "mode": 0o600,
        "command": "/opt/cct-bun/bin/bun",
        "always_load": True,
        "agent_id": "hermes-cct-canary",
        "bot_token": "",
        "pguser": "project-role",
    }


def test_hermes_tui_profile_translates_explicit_mcp_server(tmp_path: Path) -> None:
    # Arrange
    config = AgentConfig(
        name="hermes-cct-canary",
        runtime="tui",
        harness="hermes",
        workdir="/work",
        env={"PGUSER": "project-role"},
        labels={},
        mcp_servers={
            "claude-code-telegrammer": {
                "type": "stdio",
                "command": "/opt/cct-bun/bin/bun",
                "args": ["run", "/opt/cct/telegram-server.ts"],
                "alwaysLoad": True,
                "env": {
                    "CCT_BOT_TOKEN": "",
                    "CCT_AGENT_ID": "hermes-cct-canary",
                },
            }
        },
    )
    config.engine_key = "codex-subscription"
    config.model = "gpt-5.6-sol"
    config.claude.provider = ProviderSpec(
        base_url="http://gateway.example/v1",
        auth_token_env="GATEWAY_KEY",
    )
    replacements = [
        (_hermes_profile, "resolve_provider_api_key", lambda _config: "secret"),
        (_hermes_profile, "deploy_to_home", lambda _config, _target: None),
        (_hermes_profile, "deploy_to_home_overlay", lambda _config: None),
        (_hermes_profile, "resolve_overlay_upper_home", lambda _config: None),
        (
            _hermes_profile,
            "_verified_instruction_text",
            lambda _config, _targets: "canonical test instructions",
        ),
        (
            _pg_identity_credentials,
            "materialize_project_pgpass",
            lambda _config, *, home_backings, servers: None,
        ),
    ]

    # Act
    with _replace_attributes(replacements):
        targets = _hermes_profile.materialize_hermes_tui_profile(
            config, state_dir=tmp_path / "state"
        )
    home = targets[0]
    profile = yaml.safe_load(
        (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    )
    server = profile["mcp_servers"]["claude-code-telegrammer"]
    observed = {
        "mcp_file_exists": (home / ".mcp.json").is_file(),
        "profile_server_command": server["command"],
        "profile_server_pguser": server["env"]["PGUSER"],
        "eager_toolset": "mcp-claude-code-telegrammer" in profile["toolsets"],
    }

    # Assert
    assert observed == {
        "mcp_file_exists": True,
        "profile_server_command": "/opt/cct-bun/bin/bun",
        "profile_server_pguser": "project-role",
        "eager_toolset": True,
    }
