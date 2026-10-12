"""Hermes must use the compiling SAC installation across mount boundaries."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes import _fleet_env
from scitex_agent_container.runtimes import _hermes_sac_runtime as runtime


@pytest.fixture
def installed_sac(tmp_path):
    prefix = tmp_path / "environment"
    scripts = prefix / "bin"
    scripts.mkdir(parents=True)
    base = tmp_path / "python-runtime"
    base.mkdir()
    interpreter = base / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    python = scripts / "python"
    python.symlink_to(interpreter)
    binary = scripts / "sac"
    binary.write_text(f"#!{python}\n")
    binary.chmod(0o755)
    previous_get_path = runtime.sysconfig.get_path
    previous_executable = runtime.sys.executable
    previous_prefix = runtime.sys.prefix
    previous_base_prefix = runtime.sys.base_prefix
    previous_effective_env = _fleet_env.effective_env
    previous_bin = os.environ.pop("SAC_BIN", None)
    previous_bin_in_sif = os.environ.pop("SAC_BIN_IN_SIF", None)
    runtime.sysconfig.get_path = lambda name: str(scripts)
    runtime.sys.executable = str(python)
    runtime.sys.prefix = str(prefix)
    runtime.sys.base_prefix = str(base)
    _fleet_env.effective_env = lambda config: dict(config.env)
    try:
        yield runtime.SacInstallation(binary, python, prefix, base)
    finally:
        runtime.sysconfig.get_path = previous_get_path
        runtime.sys.executable = previous_executable
        runtime.sys.prefix = previous_prefix
        runtime.sys.base_prefix = previous_base_prefix
        _fleet_env.effective_env = previous_effective_env
        if previous_bin is None:
            os.environ.pop("SAC_BIN", None)
        else:
            os.environ["SAC_BIN"] = previous_bin
        if previous_bin_in_sif is None:
            os.environ.pop("SAC_BIN_IN_SIF", None)
        else:
            os.environ["SAC_BIN_IN_SIF"] = previous_bin_in_sif


def _config():
    config = AgentConfig(name="worker", harness="hermes", runtime="tui")
    config.engine_key = "muse"
    config.model = "muse-spark-1.3-contributor"
    return config


def _argv(config, installation):
    return [
        "apptainer",
        "exec",
        "--bind",
        f"{installation.prefix}:{installation.prefix}:ro",
        "--bind",
        f"{installation.base_prefix}:{installation.base_prefix}:ro",
        *runtime.sac_runtime_env_flags(config),
        "image.sif",
    ]


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes_checks_argv_str_installed_sac(installed_sac):
    # Arrange
    config = _config()
    servers = {
        "scitex-agent-container": {
            "command": "/opt/venv-sac/bin/sac",
            "args": ["mcp", "start"],
        },
        "cards": {"command": "scitex-cards"},
    }
    runtime.bind_sac_mcp_command(config, servers)
    argv = _hermes_tui_inner_argv(config)
    # Act
    # Assert
    assert argv[3] == str(installed_sac.python)


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes_checks_argv_argv_index_runtime(installed_sac):
    # Arrange
    config = _config()
    servers = {
        "scitex-agent-container": {
            "command": "/opt/venv-sac/bin/sac",
            "args": ["mcp", "start"],
        },
        "cards": {"command": "scitex-cards"},
    }
    runtime.bind_sac_mcp_command(config, servers)
    argv = _hermes_tui_inner_argv(config)
    # Act
    # Assert
    assert argv[argv.index("--", 4) + 1] == runtime.HERMES_BINARY


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes_checks_servers_scitex_agent_container(installed_sac):
    # Arrange
    config = _config()
    servers = {
        "scitex-agent-container": {
            "command": "/opt/venv-sac/bin/sac",
            "args": ["mcp", "start"],
        },
        "cards": {"command": "scitex-cards"},
    }
    runtime.bind_sac_mcp_command(config, servers)
    argv = _hermes_tui_inner_argv(config)
    # Act
    # Assert
    assert servers["scitex-agent-container"] == {
        "command": str(installed_sac.binary),
        "args": ["mcp", "start"],
    }


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes_checks_servers_cards_command_scitex(installed_sac):
    # Arrange
    config = _config()
    servers = {
        "scitex-agent-container": {
            "command": "/opt/venv-sac/bin/sac",
            "args": ["mcp", "start"],
        },
        "cards": {"command": "scitex-cards"},
    }
    runtime.bind_sac_mcp_command(config, servers)
    argv = _hermes_tui_inner_argv(config)
    # Act
    # Assert
    assert servers["cards"]["command"] == "scitex-cards"


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes_checks_runtime_validate_sac_runtime(installed_sac):
    # Arrange
    config = _config()
    servers = {
        "scitex-agent-container": {
            "command": "/opt/venv-sac/bin/sac",
            "args": ["mcp", "start"],
        },
        "cards": {"command": "scitex-cards"},
    }
    runtime.bind_sac_mcp_command(config, servers)
    argv = _hermes_tui_inner_argv(config)
    # Act
    # Assert
    assert (
        runtime.validate_sac_runtime(config, launch_argv=_argv(config, installed_sac))
        is None
    )



def test_interpreter_symlink_target_requires_its_own_visible_mount(installed_sac):
    # Arrange
    config = _config()
    argv = _argv(config, installed_sac)
    del argv[4:6]
    # Act
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)



def test_intermediate_python_version_alias_also_requires_visibility_checks_pytest_raises_runtime_hermessacruntimeerror(installed_sac, tmp_path):
    # Arrange
    interpreter = installed_sac.base_prefix / "bin" / "python3"
    interpreter.parent.mkdir()
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    alias = tmp_path / "python-version-alias"
    alias.symlink_to(installed_sac.base_prefix, target_is_directory=True)
    installed_sac.python.unlink()
    installed_sac.python.symlink_to(alias / "bin" / "python3")
    config = _config()
    argv = _argv(config, installed_sac)
    # Act
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)
    argv[-1:-1] = ["--bind", f"{alias}:{alias}:ro"]


def test_intermediate_python_version_alias_also_requires_visibility_checks_runtime_validate_sac_runtime(installed_sac, tmp_path):
    # Arrange
    interpreter = installed_sac.base_prefix / "bin" / "python3"
    interpreter.parent.mkdir()
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    alias = tmp_path / "python-version-alias"
    alias.symlink_to(installed_sac.base_prefix, target_is_directory=True)
    installed_sac.python.unlink()
    installed_sac.python.symlink_to(alias / "bin" / "python3")
    config = _config()
    argv = _argv(config, installed_sac)
    argv[-1:-1] = ["--bind", f"{alias}:{alias}:ro"]
    # Act
    # Assert
    assert runtime.validate_sac_runtime(config, launch_argv=argv) is None



def test_shadowed_specific_mount_refuses_broad_parent_visibility(installed_sac, tmp_path):
    # Arrange
    config = _config()
    impostor = tmp_path / "unrelated-install"
    impostor.mkdir()
    # Act
    argv = [
        "apptainer",
        "exec",
        "--bind",
        f"{tmp_path}:{tmp_path}:ro",
        "--bind",
        f"{impostor}:{installed_sac.prefix}",
        *runtime.sac_runtime_env_flags(config),
    ]
    # Assert
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)



def test_exact_destination_first_bind_matches_actual_apptainer_precedence(installed_sac, tmp_path):
    # Arrange
    config = _config()
    argv = _argv(config, installed_sac)
    argv[6:6] = ["--bind", f"{tmp_path}:{installed_sac.prefix}"]
    # Act
    # Assert
    assert runtime.validate_sac_runtime(config, launch_argv=argv) is None



@pytest.mark.parametrize("key", ["SAC_BIN", "SAC_BIN_IN_SIF"])
def test_override_cannot_revert_tools_to_older_image_cli(installed_sac, key):
    # Arrange
    config = _config()
    config.env[key] = "/opt/venv-sac/bin/sac"
    # Act
    # Assert
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="launching SAC installation"
    ):
        runtime.sac_installation(config)



def test_same_installation_override_is_honored(installed_sac):
    # Arrange
    config = _config()
    config.env.update(
        SAC_BIN=str(installed_sac.binary), SAC_BIN_IN_SIF=str(installed_sac.binary)
    )
    # Act
    # Assert
    assert runtime.sac_installation(config) == installed_sac



def test_raw_args_override_cannot_hide_an_older_cli(installed_sac):
    # Arrange
    config = _config()
    config.apptainer.raw_args = ["--env", "SAC_BIN=/opt/venv-sac/bin/sac"]
    # Act
    # Assert
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="launching SAC installation"
    ):
        runtime.sac_installation(config)



def test_final_flags_cannot_shadow_prepend_path(installed_sac):
    # Arrange
    config = _config()
    argv = _argv(config, installed_sac)
    argv[-1:-1] = ["--env=PREPEND_PATH=/opt/venv-sac/bin"]
    # Act
    # Assert
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="overrides the compatible SAC CLI"
    ):
        runtime.validate_sac_runtime(config, launch_argv=argv)



def test_global_unbound_prefix_does_not_fall_back_to_image_cli(installed_sac):
    # Arrange
    config = _config()
    # Act
    # Assert
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="bind the installed Python prefix"
    ):
        runtime.validate_sac_runtime(
            config,
            launch_argv=[
                "apptainer",
                "exec",
                *runtime.sac_runtime_env_flags(config),
                "image.sif",
            ],
        )



def test_missing_console_entrypoint_has_install_hint(installed_sac, tmp_path):
    # Arrange
    installed_sac.binary.unlink()
    empty = tmp_path / "empty-path"
    empty.mkdir()
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(empty)
    try:
        # Act
        call = lambda: runtime.sac_installation(_config())  # noqa: E731
        # Assert
        with pytest.raises(
            runtime.HermesSacRuntimeError, match="install scitex-agent-container"
        ):
            call()
    finally:
        os.environ["PATH"] = previous_path
