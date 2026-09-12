"""One canonical v3 spec reaches Hermes' real profile and launch argv."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import yaml

from scitex_agent_container._lifecycle._engine_select import select_engine_at_start
from scitex_agent_container._lifecycle._runtime_select import _get_runtime
from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
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
            "to_home_layers": [],
            "startup_prompts": ["Continue the assigned task."],
            "a2a": {"host": "127.0.0.1", "port": 4321},
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
                    "channels": [],
                    "background_review": True,
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


def test_real_canonical_spec_reaches_hermes_profile_and_argv(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_TEST_HERMES_ENGINE_KEY", "not-a-real-secret")
    tokenless_home = tmp_path / "tokenless-home"
    tokenless_home.mkdir()
    env_save_restore.set("HOME", str(tokenless_home))
    (tmp_path / ".scitex" / "agent-container").mkdir(parents=True)
    spec_path = tmp_path / "scholar" / "spec.yaml"
    spec_path.parent.mkdir()
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
    profile_env = (home / ".hermes" / ".env").read_text()
    rendered_argv = " ".join(argv)

    # Assert
    assert (
        isinstance(runtime, HermesTuiSessionRuntime)
        and selected.key == config.engine_key == "qwen"
        and config.model == profile["model"]["default"] == "qwen38-27b"
        and profile["providers"]["sac-qwen"]["base_url"]
        == "http://engine.example:8000/v1"
        and profile_env == "SAC_TEST_HERMES_ENGINE_KEY=not-a-real-secret\n"
        and config.claude.channels == ["server:sac"]
        and config.hermes_compression.threshold == 0.85
        and config.hermes_background_review is True
        and profile["auxiliary"]["background_review"] == {"enabled": True}
        and profile["compression"]
        == {
            "enabled": True,
            "threshold": 0.85,
            "target_ratio": 0.25,
            "tail_mode": "legacy",
            "in_place": False,
        }
        and "HERMES_HOME=/home/agent/.hermes" in argv
        and "ANTHROPIC_BASE_URL=http://engine.example:8000/v1" not in argv
        and "SAC_LISTEN_BASE_URL" not in rendered_argv
        and "SAC_LISTEN_BEARER" not in rendered_argv
        and "sac mcp channel" not in rendered_argv
        and "CLAUDE_CODE_TELEGRAMMER_TURN_URL" not in rendered_argv
        and "hermes chat --tui" in rendered_argv
        and "--continue sac:scholar" in rendered_argv
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
    spec["metadata"] = {"labels": {"project": "scitex-hub"}}
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
        and cards_env["SCITEX_STORE_DSN"]
        == "postgresql://scitex-primary:55432/scitex"
        and generated_passfile.read_text(encoding="utf-8")
        == "scitex-primary:55432:scitex:operator__scitex-hub:correct\n"
        and stat.S_IMODE(generated_passfile.stat().st_mode) == 0o600
        and "operator__scitex-hub-signup" not in cards_env.values()
        and "SAC_LISTEN_BASE_URL" not in rendered_argv
        and "SAC_LISTEN_BEARER" not in rendered_argv
        and "sac mcp channel" not in rendered_argv
    )
