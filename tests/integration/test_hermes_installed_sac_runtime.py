"""Opt-in conformance of installed SAC reuse in the actual pinned image.

Set SAC_TEST_HERMES_SIF to the reviewed existing image. This test mounts
only the installed interpreter/source and a disposable test directory; it
never mounts an agent overlay or starts a session. Source is added only to
the loader probe so development changes can be tested before wheel sealing.
The actual terminal and MCP entrypoint probes use the installed CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._hermes_sac_runtime import (
    HERMES_BINARY,
    sac_installation,
    sac_runtime_env_flags,
    validate_sac_runtime,
)


def _terminal_argv(tmp_path, config, installation, source, image):
    """Arrange the apptainer argv binding the installed SAC plus tmp state."""
    import shutil

    apptainer = shutil.which("apptainer")
    roots = dict.fromkeys(
        (
            *[
                path if path.is_dir() else path.parent
                for path in installation.required_paths
                if path.is_dir() or path.is_file()
            ],
            source,
        )
    )
    argv = [
        apptainer,
        "exec",
        "--cleanenv",
        "--containall",
        "--no-home",
        "--pwd",
        "/verification",
    ]
    for path in roots:
        argv += ["--bind", f"{path}:{path}:ro"]
    argv += [
        "--bind",
        f"{tmp_path}:/verification",
        "--env",
        "SCITEX_DIR=/verification/state",
        *sac_runtime_env_flags(config),
        image,
    ]
    return argv


def _conformance_image(tmp_path):
    """Arrange the reviewed image path; skip when the image is not provided."""
    image = os.environ.get("SAC_TEST_HERMES_SIF", "")
    if not image:
        pytest.skip("set SAC_TEST_HERMES_SIF to run actual-image conformance")
    return image


def test_existing_image_validates_installed_sac_runtime(tmp_path):
    # Arrange
    image = _conformance_image(tmp_path)
    import shutil

    config = AgentConfig(name="isolated-verification", harness="hermes", runtime="tui")
    installation = sac_installation(config)
    source = Path(__file__).resolve().parents[2] / "src"
    argv = _terminal_argv(tmp_path, config, installation, source, image)
    # Act
    validate_sac_runtime(config, launch_argv=argv)
    # Assert
    assert shutil.which("apptainer") and Path(image).is_file()


def test_existing_image_terminal_preserves_selected_sac_path(tmp_path):
    # Arrange
    image = _conformance_image(tmp_path)
    config = AgentConfig(name="isolated-verification", harness="hermes", runtime="tui")
    installation = sac_installation(config)
    source = Path(__file__).resolve().parents[2] / "src"
    argv = _terminal_argv(tmp_path, config, installation, source, image)
    validate_sac_runtime(config, launch_argv=argv)
    # Actual Hermes subprocess filtering must retain the selected SAC PATH.
    terminal_code = """
import json,pathlib,shutil,subprocess,sys
sys.path.insert(0,'/opt/hermes-agent')
from tools.environments.local import _make_run_env
env=_make_run_env({})
binary=shutil.which('sac',path=env['PATH'])
result=subprocess.run([binary,'--version'],env=env,text=True,capture_output=True)
sys.stdout.write(json.dumps({'binary':binary,'returncode':result.returncode,
 'version_output':result.stdout,'hermes_binary':str(pathlib.Path('/usr/local/bin/hermes').resolve()),
 'hermes_pin':pathlib.Path('/opt/hermes-agent/SAC_UPSTREAM_COMMIT').read_text().strip()}))
"""
    # Act
    completed = subprocess.run(
        [*argv, "/opt/hermes-agent/.venv/bin/python", "-c", terminal_code],
        text=True,
        capture_output=True,
        timeout=45,
    )
    receipt = json.loads(completed.stdout)
    # Assert
    assert (
        completed.returncode,
        receipt["binary"],
        receipt["returncode"],
        str(installation.python) in receipt["version_output"],
        receipt["hermes_binary"],
        receipt["hermes_pin"],
    ) == (
        0,
        str(installation.binary),
        0,
        True,
        HERMES_BINARY,
        os.environ.get(
            "SAC_TEST_HERMES_PIN", "e9533b0e43c2f2d8b1f6a4606db52f86481fc083"
        ),
    )


def test_existing_image_loader_parses_new_schema_with_host_interpreter(tmp_path):
    # Arrange
    image = _conformance_image(tmp_path)
    config = AgentConfig(name="isolated-verification", harness="hermes", runtime="tui")
    installation = sac_installation(config)
    source = Path(__file__).resolve().parents[2] / "src"
    argv = _terminal_argv(tmp_path, config, installation, source, image)
    validate_sac_runtime(config, launch_argv=argv)
    # The compiling source's newly added schema works with this exact host
    # interpreter in the image, without changing the image's site-packages.
    loader_code = f"""
import json,sys
sys.path.insert(0,{str(source)!r})
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_goals import HermesGoalSpec
from scitex_agent_container.runtimes._gateway_harness import HERMES_GATEWAY
config=AgentConfig(name='probe',harness='hermes',runtime='tui')
config.hermes_max_turns=99999
config.hermes_goals=HermesGoalSpec(max_turns=99999,judge_engine='same-model')
options=HERMES_GATEWAY.parse_agent_options(config)
sys.stdout.write(json.dumps(options))
"""
    # Act
    completed = subprocess.run(
        [*argv, str(installation.python), "-c", loader_code],
        text=True,
        capture_output=True,
        timeout=45,
    )
    options = json.loads(completed.stdout)
    # Assert
    assert (
        completed.returncode,
        options["max_turns"],
        options["goals"]["max_turns"],
        options["goals"]["judge_engine"],
    ) == (0, 99999, 99999, "same-model")
