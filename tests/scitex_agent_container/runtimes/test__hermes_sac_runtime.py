"""Hermes must use the compiling SAC installation across mount boundaries."""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes import _fleet_env
from scitex_agent_container.runtimes import _hermes_sac_runtime as runtime


@pytest.fixture
def installed_sac(tmp_path, monkeypatch):
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
    monkeypatch.setattr(runtime.sysconfig, "get_path", lambda name: str(scripts))
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime.sys, "base_prefix", str(base))
    monkeypatch.setattr(_fleet_env, "effective_env", lambda config: dict(config.env))
    monkeypatch.delenv("SAC_BIN", raising=False)
    monkeypatch.delenv("SAC_BIN_IN_SIF", raising=False)
    return runtime.SacInstallation(binary, python, prefix, base)


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


def test_owner_and_mcp_use_installed_sac_but_keep_image_hermes(installed_sac):
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
    assert argv[3] == str(installed_sac.python)
    assert argv[argv.index("--", 4) + 1] == runtime.HERMES_BINARY
    assert servers["scitex-agent-container"] == {
        "command": str(installed_sac.binary),
        "args": ["mcp", "start"],
    }
    assert servers["cards"]["command"] == "scitex-cards"
    assert (
        runtime.validate_sac_runtime(config, launch_argv=_argv(config, installed_sac))
        is None
    )


def test_interpreter_symlink_target_requires_its_own_visible_mount(installed_sac):
    config = _config()
    argv = _argv(config, installed_sac)
    del argv[4:6]
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)


def test_intermediate_python_version_alias_also_requires_visibility(
    installed_sac, tmp_path
):
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
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)
    argv[-1:-1] = ["--bind", f"{alias}:{alias}:ro"]
    assert runtime.validate_sac_runtime(config, launch_argv=argv) is None


def test_shadowed_specific_mount_refuses_broad_parent_visibility(
    installed_sac, tmp_path
):
    config = _config()
    impostor = tmp_path / "unrelated-install"
    impostor.mkdir()
    argv = [
        "apptainer",
        "exec",
        "--bind",
        f"{tmp_path}:{tmp_path}:ro",
        "--bind",
        f"{impostor}:{installed_sac.prefix}",
        *runtime.sac_runtime_env_flags(config),
    ]
    with pytest.raises(runtime.HermesSacRuntimeError, match="absent or shadowed"):
        runtime.validate_sac_runtime(config, launch_argv=argv)


def test_exact_destination_first_bind_matches_actual_apptainer_precedence(
    installed_sac, tmp_path
):
    config = _config()
    argv = _argv(config, installed_sac)
    argv[6:6] = ["--bind", f"{tmp_path}:{installed_sac.prefix}"]
    assert runtime.validate_sac_runtime(config, launch_argv=argv) is None


@pytest.mark.parametrize("key", ["SAC_BIN", "SAC_BIN_IN_SIF"])
def test_override_cannot_revert_tools_to_older_image_cli(installed_sac, key):
    config = _config()
    config.env[key] = "/opt/venv-sac/bin/sac"
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="launching SAC installation"
    ):
        runtime.sac_installation(config)


def test_same_installation_override_is_honored(installed_sac):
    config = _config()
    config.env.update(
        SAC_BIN=str(installed_sac.binary), SAC_BIN_IN_SIF=str(installed_sac.binary)
    )
    assert runtime.sac_installation(config) == installed_sac


def test_raw_args_override_cannot_hide_an_older_cli(installed_sac):
    config = _config()
    config.apptainer.raw_args = ["--env", "SAC_BIN=/opt/venv-sac/bin/sac"]
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="launching SAC installation"
    ):
        runtime.sac_installation(config)


def test_final_flags_cannot_shadow_prepend_path(installed_sac):
    config = _config()
    argv = _argv(config, installed_sac)
    argv[-1:-1] = ["--env=PREPEND_PATH=/opt/venv-sac/bin"]
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="overrides the compatible SAC CLI"
    ):
        runtime.validate_sac_runtime(config, launch_argv=argv)


def test_global_unbound_prefix_does_not_fall_back_to_image_cli(installed_sac):
    config = _config()
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


def test_missing_console_entrypoint_has_install_hint(installed_sac):
    installed_sac.binary.unlink()
    with pytest.raises(
        runtime.HermesSacRuntimeError, match="install scitex-agent-container"
    ):
        runtime.sac_installation(_config())
