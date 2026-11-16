"""Creation/start diagnostics through the real CLI in an isolated owned home."""

import json
import os
import subprocess
import sys

import pytest

_CHILD = r"""
import hashlib
import json
import sys
from pathlib import Path

blocked = []
def guard(event, args):
    if event in {"socket.connect", "subprocess.Popen"}:
        blocked.append(event)
        raise RuntimeError("fixture forbids network and process launch")
sys.addaudithook(guard)

import click
from scitex_agent_container._mcp._tools._agent import agent_create

scenario = sys.argv[1]
spec = Path("state/agent-container/agents/owned-streams/spec.yaml")
if scenario == "preview":
    import scitex_agent_container as sac
    result = sac.agent.create("owned-streams", template="local-not-checked", start=True, dry_run=True)
    unchanged = None
elif scenario == "duplicate":
    agent_create("owned-streams", template="minimal", start=False)
    before = hashlib.sha256(spec.read_bytes()).hexdigest()
    try:
        result = agent_create("owned-streams", template="minimal", start=True)
    except click.ClickException as exc:
        result = {"refusal": str(exc)}
    unchanged = before == hashlib.sha256(spec.read_bytes()).hexdigest()
else:
    result = agent_create("owned-streams", template="minimal", start=scenario == "start")
    unchanged = None
print(json.dumps({"result": result, "blocked": blocked,
                  "spec_exists": spec.is_file(), "unchanged": unchanged}))
"""


def _invoke_owned_case(tmp_path, scenario):
    """Run the actual public wrapper and CLI with no inherited host authority."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "SCITEX_DIR": str(state),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SCITEX_AGENT_CONTAINER_HOSTNAME": "owned-local-fixture",
        "SAC_BUILD_NO_NICE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }
    env.update(
        {
            key: os.environ[key]
            for key in ("COVERAGE_PROCESS_START", "COVERAGE_FILE")
            if key in os.environ
        }
    )

    process = subprocess.run(
        [sys.executable, "-c", _CHILD, scenario],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return process, json.loads(process.stdout)


@pytest.mark.parametrize(
    ("scenario", "exit_code", "oauth_refusal"),
    [("create", 0, False), ("start", 1, True)],
)
def test_agent_create_preserves_cli_diagnostics(
    tmp_path, scenario, exit_code, oauth_refusal
):
    # Arrange
    expected = (0, "", [], True, exit_code, True, oauth_refusal)

    # Act
    process, payload = _invoke_owned_case(tmp_path, scenario)
    result = payload["result"]
    stderr = result.get("stderr", "")

    # Assert
    assert (
        process.returncode,
        process.stderr,
        payload["blocked"],
        payload["spec_exists"],
        result.get("exit_code"),
        "no account credentials found" in stderr,
        "OAuth credentials file not found" in stderr,
    ) == expected


def test_agent_create_refuses_overwrite_before_requested_start(tmp_path):
    # Arrange
    expected = (0, "", [], True, True, True)

    # Act
    process, payload = _invoke_owned_case(tmp_path, "duplicate")

    # Assert
    assert (
        process.returncode,
        process.stderr,
        payload["blocked"],
        payload["spec_exists"],
        payload["unchanged"],
        "Refusing to overwrite existing spec" in payload["result"].get("refusal", ""),
    ) == expected


def test_agent_create_preview_never_scaffolds_or_starts(tmp_path):
    # Arrange
    expected = (0, "", [], False, 0, "", True, "arguments_only", "local-not-checked")

    # Act
    process, payload = _invoke_owned_case(tmp_path, "preview")
    result = payload["result"]
    plan = json.loads(result["stdout"])

    # Assert
    assert (
        process.returncode,
        process.stderr,
        payload["blocked"],
        payload["spec_exists"],
        result["exit_code"],
        result["stderr"],
        plan["dry_run"],
        plan["scope"],
        plan["template"],
    ) == expected
