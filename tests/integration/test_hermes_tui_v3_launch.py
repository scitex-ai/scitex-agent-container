"""One canonical v3 spec reaches Hermes' real profile and launch argv."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import yaml

from scitex_agent_container._lifecycle._engine_select import select_engine_at_start
from scitex_agent_container._lifecycle._runtime_select import _get_runtime
from scitex_agent_container._listen.tokens import default_token_path
from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_inner_argv_tui import (
    tui_channel_config,
)
from scitex_agent_container.runtimes._hermes_profile import (
    validate_hermes_tui_profile,
)
from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


def _canonical_hermes_spec() -> dict:
    doc = explicit_doc(
        {
            "harness": "hermes",
            "runtime": "tui",
            "engine": "qwen",
            "workdir": "/work",
            "to_home": "",
            "to_home_layers": ["per-agent"],
            "startup_prompts": ["Continue the assigned task."],
            "a2a": {"host": "127.0.0.1", "port": 4321},
            "comms": {
                "channels": [],
                "outbound": {"siblings": "allow", "parent": "allow"},
                "inbound": {"siblings": "allow", "parent": "allow"},
                "a2a": {"listen": True},
            },
            "available_engines": {
                "qwen": {
                    "model": "qwen38-27b",
                    "provider": {
                        "base_url": "http://engine.example:8000/v1",
                        "auth_token_env": "SAC_TEST_HERMES_ENGINE_KEY",
                    },
                    "reasoning_effort": "low",
                    "max_context_tokens": 1_048_576,
                },
                "fallback": {
                    "model": "fallback-model",
                    "provider": {
                        "base_url": "http://fallback.example:8000/v1",
                        "auth_token_env": "SAC_TEST_HERMES_ENGINE_KEY",
                    },
                },
            },
            "available_harnesses": {
                "hermes": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "background_review": True,
                    "run_budget_seconds": 90,
                    "compression": {
                        "threshold": 0.85,
                        "target_ratio": 0.25,
                        "tail_mode": "legacy",
                        "in_place": False,
                    },
                }
            },
        }
    )
    # Canonical authoring omits the compatibility fields materialized by the
    # v3 boundary for existing internal consumers.
    for legacy_key in ("engines", "claude", "watchdog", "container"):
        doc["spec"].pop(legacy_key, None)
    return doc


def _canonical_codex_spec() -> dict:
    doc = explicit_doc(
        {
            "harness": "codex",
            "runtime": "tui",
            "engine": "qwen",
            "workdir": "/work",
            "to_home": "",
            "to_home_layers": [],
            "startup_prompts": ["Continue the assigned task."],
            "a2a": {"host": "127.0.0.1", "port": 4321},
            "comms": {
                "channels": ["server:sac"],
                "outbound": {"siblings": "allow", "parent": "allow"},
                "inbound": {"siblings": "allow", "parent": "allow"},
                "a2a": {"listen": True},
            },
            "available_engines": {
                "qwen": {
                    "model": "qwen38-27b",
                    "provider": {
                        "base_url": "http://engine.example:8000/v1",
                        "auth_token_env": "SAC_TEST_CODEX_ENGINE_KEY",
                    },
                    "reasoning_effort": "low",
                    "max_context_tokens": 1_048_576,
                }
            },
            "available_harnesses": {
                "codex": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "approval_policy": "never",
                    "sandbox_mode": "danger-full-access",
                }
            },
        }
    )
    for legacy_key in ("engines", "claude", "watchdog", "container"):
        doc["spec"].pop(legacy_key, None)
    return doc


def test_real_canonical_codex_spec_reaches_the_sac_channel_adapter(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_TEST_CODEX_ENGINE_KEY", "not-a-real-secret")
    spec_path = tmp_path / "codex-worker" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(_canonical_codex_spec(), sort_keys=False), encoding="utf-8"
    )

    # Act
    config = load_config(spec_path)
    dev_channels, channel_mcp = tui_channel_config(config)

    # Assert
    mcp = json.loads(channel_mcp or "{}")
    sidecar = mcp["mcpServers"]["sac"]
    assert (
        config.comms.channels,
        config.claude.channels,
        dev_channels,
        sidecar["args"][-1],
    ) == (["server:sac"], ["server:sac"], None, "--send-only")


def test_real_canonical_spec_reaches_hermes_profile_and_argv(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_TEST_HERMES_ENGINE_KEY", "not-a-real-secret")
    tokenless_home = tmp_path / "tokenless-home"
    tokenless_home.mkdir()
    env_save_restore.set("HOME", str(tokenless_home))
    env_save_restore.set("LOGNAME", "operator")
    env_save_restore.set("USER", "operator")
    env_save_restore.delete("PGPASSFILE")
    source_passfile = tokenless_home / ".pgpass"
    source_passfile.write_text(
        "scitex-primary:55432:scitex:operator__scholar:test-password\n",
        encoding="utf-8",
    )
    source_passfile.chmod(0o600)
    listen_token = default_token_path()
    listen_token.parent.mkdir(parents=True)
    listen_token.write_text("test-listen-bearer\n", encoding="utf-8")
    listen_token.chmod(0o600)
    (tmp_path / ".scitex" / "agent-container").mkdir(parents=True)
    spec_path = tmp_path / "scholar" / "spec.yaml"
    spec_path.parent.mkdir()
    to_home = spec_path.parent / "to_home"
    to_home.mkdir()
    (to_home / "AGENTS.md").write_text("canonical test instructions\n")
    spec_path.write_text(
        yaml.safe_dump(_canonical_hermes_spec(), sort_keys=False), encoding="utf-8"
    )

    # Act
    config = load_config(spec_path)
    selected = select_engine_at_start(config, None, log=False)
    runtime = _get_runtime(config)
    home = runtime.materialize_workspace(config)
    state_dir = home.parent
    argv = build_run_argv(
        config,
        state_dir=state_dir,
        sif_path=Path("/images/sac-base.sif"),
        tui=True,
    )
    profile = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    profile_env = dict(
        line.split("=", 1)
        for line in (home / ".hermes" / ".env").read_text().splitlines()
    )
    rendered_argv = " ".join(argv)

    # Assert
    assert (
        isinstance(runtime, HermesTuiSessionRuntime)
        and selected.key == config.engine_key == "qwen"
        and config.model == profile["model"]["default"] == "qwen38-27b"
        and profile["providers"]["sac-qwen"]["base_url"]
        == "http://engine.example:8000/v1"
        and profile_env
        == {
            "SAC_TEST_HERMES_ENGINE_KEY": "not-a-real-secret",
            "SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878",
            "SAC_LISTEN_BEARER": "test-listen-bearer",
            "SAC_NAME": "scholar",
        }
        and set(profile["mcp_servers"]) == {"scitex-agent-container"}
        and config.claude.channels == ["server:sac"]
        and config.comms.channels == ["server:sac"]
        and config.hermes_compression.threshold == 0.85
        and config.hermes_background_review is True
        and config.hermes_run_budget_seconds == 90
        and profile["auxiliary"]["background_review"] == {"enabled": True}
        and profile["agent"]["run_budget_seconds"] == 90
        and profile["agent"]["max_turns"] == "none"
        and profile["compression"]
        == {
            "enabled": True,
            "threshold": 0.85,
            "target_ratio": 0.25,
            "tail_mode": "legacy",
            "in_place": False,
        }
        and "HERMES_HOME=/home/agent/.hermes" in argv
        and str(home / ".hermes" / ".env") in argv
        and "ANTHROPIC_BASE_URL=http://engine.example:8000/v1" not in argv
        and "SAC_LISTEN_BASE_URL" not in rendered_argv
        and "SAC_LISTEN_BEARER" not in rendered_argv
        and "sac mcp channel" not in rendered_argv
        and "CLAUDE_CODE_TELEGRAMMER_TURN_URL" not in rendered_argv
        and "hermes chat --tui" in rendered_argv
        and "--model qwen38-27b --provider custom:sac-qwen" in rendered_argv
        and "--continue sac:scholar:qwen --create-if-missing" in rendered_argv
    )


def test_real_hermes_cct_launch_wires_mcp_and_tui_turn_bridge(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_TEST_HERMES_ENGINE_KEY", "not-a-real-secret")
    tokenless_home = tmp_path / "tokenless-home"
    tokenless_home.mkdir()
    env_save_restore.set("HOME", str(tokenless_home))
    env_save_restore.set("LOGNAME", "operator")
    env_save_restore.set("USER", "operator")
    env_save_restore.delete("PGPASSFILE")
    source_passfile = tokenless_home / ".pgpass"
    source_passfile.write_text(
        "scitex-primary:55432:scitex:operator__business:test-password\n",
        encoding="utf-8",
    )
    source_passfile.chmod(0o600)
    to_home = tmp_path / "to_home"
    to_home.mkdir()
    (to_home / "AGENTS.md").write_text("canonical test instructions\n")
    (to_home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "claude-code-telegrammer": {
                        "command": "bun",
                        "args": ["run", "telegram-server.ts"],
                        "env": {"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"},
                    },
                    "optional-browser": {"command": "browser-mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    doc = _canonical_hermes_spec()
    doc["metadata"] = {"labels": {"sac-builtin": "off"}}
    doc["spec"]["to_home"] = str(to_home)
    doc["spec"]["comms"]["channels"] = ["server:claude-code-telegrammer"]
    doc["spec"]["apptainer"]["env"]["CCT_BOT_TOKEN"] = "test-cct-secret"
    doc["spec"]["apptainer"]["env"]["SCITEX_STORE_DSN"] = (
        "postgresql://scitex-primary:55432/scitex"
    )
    spec_path = tmp_path / "business" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    # Act
    config = load_config(spec_path)
    select_engine_at_start(config, None, log=False)
    runtime = _get_runtime(config)
    home = runtime.materialize_workspace(config)
    argv = build_run_argv(
        config,
        state_dir=home.parent,
        sif_path=Path("/images/sac-base.sif"),
        tui=True,
    )
    rendered = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    cct_env = rendered["mcp_servers"]["claude-code-telegrammer"]["env"]
    joined = " ".join(argv)

    # Assert
    assert (
        set(rendered["mcp_servers"]),
        rendered["mcp_servers"]["claude-code-telegrammer"]["env"][
            "CLAUDE_CODE_TELEGRAMMER_TURN_URL"
        ],
        "CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:4321/v1/turn" in joined,
        "test-cct-secret" not in joined,
        cct_env["SCITEX_STORE_DSN"],
        cct_env["PGUSER"],
        cct_env["PGPASSFILE"],
    ) == (
        {"claude-code-telegrammer"},
        "http://127.0.0.1:4321/v1/turn",
        True,
        True,
        "postgresql://scitex-primary:55432/scitex",
        "operator__business",
        "/home/agent/.sac-pgpass",
    )


def test_real_hermes_launch_provisions_exact_project_pg_identity(
    tmp_path, env_save_restore
):
    """The canonical launch path must not invent a role for a variant name."""
    # Arrange
    env_save_restore.set("SAC_TEST_HERMES_ENGINE_KEY", "not-a-real-secret")
    tokenless_home = tmp_path / "tokenless-home"
    tokenless_home.mkdir()
    env_save_restore.set("HOME", str(tokenless_home))
    env_save_restore.set("LOGNAME", "operator")
    env_save_restore.set("USER", "operator")
    env_save_restore.delete("PGPASSFILE")
    source_passfile = tokenless_home / ".pgpass"
    source_passfile.write_text(
        "scitex-primary:55432:scitex:operator__scitex-hub-signup:wrong\n"
        "scitex-primary:55432:scitex:operator__scitex-hub:correct\n",
        encoding="utf-8",
    )
    source_passfile.chmod(0o600)
    to_home = tmp_path / "to_home"
    to_home.mkdir()
    (to_home / "AGENTS.md").write_text("canonical test instructions\n")
    (to_home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "scitex-cards": {
                        "command": "scitex-cards",
                        "args": ["mcp", "start"],
                        "alwaysLoad": True,
                        "env": {
                            "SCITEX_STORE_DSN": "${SCITEX_STORE_DSN}",
                            "PGUSER": "${PGUSER}",
                            "PGPASSFILE": "${PGPASSFILE}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    spec = _canonical_hermes_spec()
    spec["metadata"] = {"labels": {"project": "scitex-hub", "sac-builtin": "off"}}
    spec["spec"]["to_home"] = str(to_home)
    spec_path = tmp_path / "scitex-hub-signup" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    # Act: this is the production config -> runtime -> profile -> argv seam.
    config = load_config(spec_path)
    select_engine_at_start(config, None, log=False)
    runtime = _get_runtime(config)
    home = runtime.materialize_workspace(config)
    state_dir = home.parent
    argv = build_run_argv(
        config,
        state_dir=state_dir,
        sif_path=Path("/images/sac-base.sif"),
        tui=True,
    )
    validate_hermes_tui_profile(config, state_dir=state_dir, launch_argv=argv)
    profile = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    cards_env = profile["mcp_servers"]["scitex-cards"]["env"]
    generated_passfile = home / ".sac-pgpass"
    rendered_argv = " ".join(argv)

    # Assert
    assert (
        isinstance(runtime, HermesTuiSessionRuntime)
        and cards_env["PGUSER"] == "operator__scitex-hub"
        and cards_env["PGPASSFILE"] == "/home/agent/.sac-pgpass"
        and cards_env["SCITEX_STORE_DSN"] == "postgresql://scitex-primary:55432/scitex"
        and generated_passfile.read_text(encoding="utf-8")
        == "scitex-primary:55432:scitex:operator__scitex-hub:correct\n"
        and stat.S_IMODE(generated_passfile.stat().st_mode) == 0o600
        and "operator__scitex-hub-signup" not in cards_env.values()
        and "SAC_LISTEN_BASE_URL" not in rendered_argv
        and "SAC_LISTEN_BEARER" not in rendered_argv
        and "sac mcp channel" not in rendered_argv
    )
