"""A sealed (spec.sandbox) Hermes run hides SAC's control plane from the agent."""

from __future__ import annotations

import pytest

from scitex_agent_container._sandbox import parse_sandbox
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes import _hermes_sac_runtime as runtime

from . import test__hermes_sac_runtime as unsealed

_argv, _config = unsealed._argv, unsealed._config
#: The same installed-SAC fixture the unsealed tests use.
installed_sac = unsealed.installed_sac


def _sealed_config():
    config = _config()
    config.sandbox = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["example.org:443"]},
                "env": {"allow": [], "model_key": "MODEL_KEY"},
                "run": {"root": "/scratch/runs", "size": "1G", "after": "archive"},
            }
        }
    )
    return config


def _hidden(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--hide"]


def test_sealed_run_puts_no_sac_on_the_agent_path(installed_sac):
    # Arrange
    config = _sealed_config()
    # Act
    flags = runtime.sac_runtime_env_flags(config)
    # Assert
    assert flags == []


def test_unsealed_run_still_prepends_the_sac_scripts(installed_sac):
    # Arrange
    config = _config()
    # Act
    flags = runtime.sac_runtime_env_flags(config)
    # Assert
    assert f"PREPEND_PATH={installed_sac.binary.parent}" in flags


def test_sealed_owner_hides_the_agent_state_dir(installed_sac):
    # Arrange
    config = _sealed_config()
    # Act
    hidden = _hidden(_hermes_tui_inner_argv(config))
    # Assert
    assert "/state/worker" in hidden


def test_sealed_owner_hides_the_launching_sac_environment(installed_sac):
    # Arrange
    config = _sealed_config()
    # Act
    hidden = _hidden(_hermes_tui_inner_argv(config))
    # Assert
    assert str(installed_sac.prefix) in hidden


def test_sealed_owner_hides_the_in_image_sac(installed_sac):
    # Arrange
    config = _sealed_config()
    # Act
    hidden = _hidden(_hermes_tui_inner_argv(config))
    # Assert
    assert "/opt/venv-sac/bin/sac" in hidden


def test_sealed_owner_hides_before_the_hermes_command(installed_sac):
    # Arrange
    config = _sealed_config()
    argv = _hermes_tui_inner_argv(config)
    # Act
    last_hide = max(i for i, a in enumerate(argv) if a == "--hide")
    # Assert
    assert argv[last_hide + 2 : last_hide + 4] == ["--", runtime.HERMES_BINARY]


def test_unsealed_owner_hides_nothing(installed_sac):
    # Arrange
    config = _config()
    # Act
    hidden = _hidden(_hermes_tui_inner_argv(config))
    # Assert
    assert hidden == []


def test_sealed_finalized_argv_without_sac_env_is_valid(installed_sac):
    # Arrange
    config = _sealed_config()
    argv = _argv(config, installed_sac)
    # Act
    result = runtime.validate_sac_runtime(config, launch_argv=argv)
    # Assert
    assert result is None


def test_sealed_argv_pointing_the_agent_at_sac_is_refused(installed_sac):
    # Arrange
    config = _sealed_config()
    argv = _argv(config, installed_sac)
    argv[-1:-1] = ["--env", f"SAC_BIN={installed_sac.binary}"]
    # Act
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="SAC_BIN"):
        runtime.validate_sac_runtime(config, launch_argv=argv)


def test_sealed_argv_prepending_sac_scripts_is_refused(installed_sac):
    # Arrange
    config = _sealed_config()
    argv = _argv(config, installed_sac)
    argv[-1:-1] = ["--env", f"PREPEND_PATH={installed_sac.binary.parent}"]
    # Act
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="PREPEND_PATH"):
        runtime.validate_sac_runtime(config, launch_argv=argv)


def test_sealed_run_refuses_a_sac_mcp_server(installed_sac):
    # Arrange
    config = _sealed_config()
    servers = {"sac": {"command": "sac", "args": ["mcp", "start"]}}
    # Act
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="hides SAC"):
        runtime.bind_sac_mcp_command(config, servers)
