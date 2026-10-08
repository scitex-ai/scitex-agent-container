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
import shutil
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


def test_existing_image_executes_installed_sac_in_hermes_terminal(tmp_path):
    image = os.environ.get("SAC_TEST_HERMES_SIF", "")
    if not image:
        pytest.skip("set SAC_TEST_HERMES_SIF to run actual-image conformance")
    apptainer = shutil.which("apptainer")
    assert apptainer and Path(image).is_file()
    config = AgentConfig(name="isolated-verification", harness="hermes", runtime="tui")
    installation = sac_installation(config)
    source = Path(__file__).resolve().parents[2] / "src"
    roots = dict.fromkeys(
        (*[path for path in installation.required_paths if path.is_dir()], source)
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
    completed = subprocess.run(
        [*argv, "/opt/hermes-agent/.venv/bin/python", "-c", terminal_code],
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["binary"] == str(installation.binary)
    assert receipt["returncode"] == 0
    assert str(installation.python) in receipt["version_output"]
    assert receipt["hermes_binary"] == HERMES_BINARY
    assert receipt["hermes_pin"] == os.environ.get(
        "SAC_TEST_HERMES_PIN", "17c5fde5a3f3642262003cd6aa09d54cf4d11de3"
    )

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
    completed = subprocess.run(
        [*argv, str(installation.python), "-c", loader_code],
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    options = json.loads(completed.stdout)
    assert options["max_turns"] == options["goals"]["max_turns"] == 99999
    assert options["goals"]["judge_engine"] == "same-model"
