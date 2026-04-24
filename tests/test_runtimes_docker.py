"""Real-Docker smoke tests for DockerRuntime + newbie-docker template.

Skipped unless Docker is actually installed and the daemon is reachable.
Opt-in: runs with ``pytest -m docker_smoke``.

These tests spin up real containers and can take 10-60s each. They are
intentionally not part of the default ``pytest`` run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

# Gate: docker CLI on PATH and daemon responsive
_HAS_DOCKER_CLI = shutil.which("docker") is not None
if _HAS_DOCKER_CLI:
    _probe = subprocess.run(
        ["docker", "version"],
        capture_output=True,
        timeout=5,
    )
    _DOCKER_OK = _probe.returncode == 0
else:
    _DOCKER_OK = False

# Individual tests that need a live docker daemon are decorated
# per-function with ``@pytest.mark.docker_smoke`` + skipif; the fast
# unit tests at the bottom run unconditionally.

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "config" / "templates" / "newbie-docker.yaml"
TEST_IMAGE = "scitex-agent-container:test"
AGENT_IMAGE = "scitex-agent-container:latest"


def _docker(*args: str, check: bool = False, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, **kw
    )


def _image_exists(ref: str) -> bool:
    res = _docker("image", "inspect", ref)
    return res.returncode == 0


# ---------------------------------------------------------------------------
# a. Build image from containers/
# ---------------------------------------------------------------------------


@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
def test_build_image_from_containers_dir(request):
    """DockerRuntime.build_image() on containers/ must succeed and register image."""
    from scitex_agent_container.runtimes.docker import DockerRuntime

    context = str(REPO_ROOT / "containers")

    def _rmi():
        _docker("rmi", "-f", TEST_IMAGE)

    request.addfinalizer(_rmi)

    ok = DockerRuntime.build_image(image=TEST_IMAGE, context=context)
    assert ok is True, "build_image returned False"
    assert _image_exists(TEST_IMAGE), f"{TEST_IMAGE} not registered with docker"


# ---------------------------------------------------------------------------
# b. Start/stop the newbie-docker agent
# ---------------------------------------------------------------------------


def _load_newbie_config_with_override(tmp_path: Path, override_name: str):
    """Copy the template into a throwaway dir-as-SSoT layout so load_config
    picks up ``override_name`` as the agent name.
    """
    from scitex_agent_container.config import load_config

    agent_dir = tmp_path / override_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / f"{override_name}.yaml"

    raw = yaml.safe_load(TEMPLATE.read_text())
    target.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(target)


@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
def test_start_and_stop_newbie_docker_agent(request, tmp_path):
    """Start the newbie-docker agent, verify is_running, then stop it."""
    from scitex_agent_container.runtimes.docker import DockerRuntime

    if not _image_exists(AGENT_IMAGE):
        pytest.skip(f"{AGENT_IMAGE} not built on this host")

    agent_name = f"newbie-docker-test-{os.getpid()}"
    config = _load_newbie_config_with_override(tmp_path, agent_name)
    runtime = DockerRuntime()
    container = runtime._container_name(config)

    def _teardown():
        _docker("rm", "-f", container)

    request.addfinalizer(_teardown)

    started = runtime.start(config)
    assert started, (
        f"DockerRuntime.start() returned False; logs:\n{runtime.logs(config)}"
    )

    # Poll is_running up to 10s
    deadline = time.time() + 10
    while time.time() < deadline:
        if runtime.is_running(config):
            break
        time.sleep(0.5)
    assert runtime.is_running(config), "container did not reach running state"

    runtime.stop(config)
    assert not runtime.is_running(config), "container still running after stop()"


# ---------------------------------------------------------------------------
# c. `claude -p "hello"` inside the container
# ---------------------------------------------------------------------------


@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
@pytest.mark.xfail(
    reason=(
        "Requires claude CLI to accept non-interactive -p inside the container "
        "image AND a configured auth source. If it fails, the Dockerfile needs "
        "either an ANTHROPIC_API_KEY env forwarded or bundled auth — fix by "
        "passing `-e ANTHROPIC_API_KEY` in DockerRuntime._build_docker_args "
        "or by adjusting the image entrypoint."
    ),
    strict=False,
)
def test_claude_p_hello_inside_container(request, tmp_path):
    from scitex_agent_container.runtimes.docker import DockerRuntime

    if not _image_exists(AGENT_IMAGE):
        pytest.skip(f"{AGENT_IMAGE} not built on this host")

    agent_name = f"newbie-docker-exec-{os.getpid()}"
    config = _load_newbie_config_with_override(tmp_path, agent_name)
    runtime = DockerRuntime()
    container = runtime._container_name(config)

    def _teardown():
        _docker("rm", "-f", container)

    request.addfinalizer(_teardown)

    assert runtime.start(config), f"start failed: {runtime.logs(config)}"

    # Give the container a moment to finish its first readiness
    time.sleep(2)

    res = _docker(
        "exec",
        container,
        "claude",
        "-p",
        "hello",
        "--output-format",
        "json",
        "--model",
        "claude-haiku-4-5",
        timeout=60,
    )
    assert res.returncode == 0, f"claude -p failed: {res.stderr}"
    envelope = json.loads(res.stdout)
    assert envelope.get("type") == "result"
    assert envelope.get("is_error") is False


# ---------------------------------------------------------------------------
# d. Fast unit tests for opt-in ~/.claude auto-mount
#    (no container start — inspect _build_docker_args() output)
# ---------------------------------------------------------------------------


def _minimal_docker_config(tmp_path: Path, name: str, mount_host_claude: bool):
    """Build a v3-valid config with docker runtime and the given flag."""
    from scitex_agent_container.config import load_config

    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / f"{name}.yaml"
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "claude-code",
            "model": "sonnet",
            "container": {
                "runtime": "docker",
                "image": "scitex-agent-container:latest",
                "network": "none",
                "mount_host_claude": mount_host_claude,
            },
        },
    }
    target.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(target)


def test_mount_host_claude_defaults_to_false(tmp_path):
    """With mount_host_claude omitted, no -v for ~/.claude must appear."""
    from scitex_agent_container.runtimes.docker import DockerRuntime

    # Build a config with the flag explicitly False (== default).
    config = _minimal_docker_config(tmp_path, "no-claude-mount", False)
    args = DockerRuntime()._build_docker_args(config)

    # None of the -v payloads should reference the host ~/.claude dir.
    joined = " ".join(args)
    assert "/.claude:/home/agent/.claude" not in joined, (
        f"expected no ~/.claude auto-mount, got args: {args}"
    )


def test_mount_host_claude_true_mounts_ro(tmp_path):
    """With mount_host_claude=True, the -v <HOME>/.claude:...:ro must appear."""
    from scitex_agent_container.runtimes.docker import DockerRuntime

    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        pytest.skip(f"host {claude_dir} does not exist; runtime only mounts if it does")

    config = _minimal_docker_config(tmp_path, "yes-claude-mount", True)
    args = DockerRuntime()._build_docker_args(config)

    expected = f"{claude_dir}:/home/agent/.claude:ro"
    assert expected in args, f"expected {expected!r} in docker args, got: {args}"
