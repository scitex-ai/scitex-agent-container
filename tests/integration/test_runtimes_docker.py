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

_CI_KEY_SET = bool(os.environ.get("SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY"))

# Individual tests that need a live docker daemon are decorated
# per-function with ``@pytest.mark.docker_smoke`` + skipif; the fast
# unit tests at the bottom run unconditionally.

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "config" / "examples" / "newbie-docker.yaml"
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
# Session-scoped fixture: build the test image once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_image():
    """Build ``scitex-agent-container:test`` once per session. Skip if no docker."""
    if not _DOCKER_OK:
        pytest.skip("docker unavailable (CLI or daemon)")
    from scitex_agent_container.runtimes.docker import DockerRuntime

    context = str(REPO_ROOT / "containers")
    dockerfile = str(REPO_ROOT / "containers" / "Dockerfile.sdk-persistent")
    if not _image_exists(TEST_IMAGE):
        ok = DockerRuntime.build_image(
            image=TEST_IMAGE, context=context, dockerfile=dockerfile
        )
        if not ok or not _image_exists(TEST_IMAGE):
            pytest.fail(f"Failed to build {TEST_IMAGE} from {context}")
    return TEST_IMAGE


def _run_bare_container(image: str, name: str) -> None:
    """Run a sleep-infinity container from ``image`` with CI key forwarded.

    Uses --entrypoint to override the image's default ``claude`` entrypoint,
    so the container stays alive long enough to ``docker exec`` into it.
    """
    _docker("rm", "-f", name)
    env_args: list[str] = []
    ci_key = os.environ.get("SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY")
    if ci_key:
        env_args = ["-e", f"ANTHROPIC_API_KEY={ci_key}"]
    res = _docker(
        "run",
        "-d",
        "--name",
        name,
        "--entrypoint",
        "sleep",
        *env_args,
        image,
        "infinity",
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker run failed: {res.stderr}")


# ---------------------------------------------------------------------------
# a. Build image from containers/
# ---------------------------------------------------------------------------


@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
def test_build_image_from_containers_dir(test_image):
    """DockerRuntime.build_image() produced an image with ``claude`` on PATH."""
    assert _image_exists(test_image), f"{test_image} not registered with docker"
    res = _docker("run", "--rm", "--entrypoint", "which", test_image, "claude")
    assert res.returncode == 0, f"`which claude` failed: {res.stderr}"
    assert res.stdout.strip(), "claude not on PATH inside image"


# ---------------------------------------------------------------------------
# b. Start/stop the newbie-docker agent
# ---------------------------------------------------------------------------


def _load_newbie_config_with_override(
    tmp_path: Path, override_name: str, image: str | None = None
):
    """Copy the template into a throwaway dir-as-SSoT layout so load_config
    picks up ``override_name`` as the agent name. Optionally override image.
    """
    from scitex_agent_container.config import load_config

    agent_dir = tmp_path / override_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / f"{override_name}.yaml"

    raw = yaml.safe_load(TEMPLATE.read_text())
    if image is not None:
        # F-CS16 phase 2a: image is now top-level, not nested.
        raw["spec"]["image"] = image
    target.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(target)


@pytest.mark.skip(
    reason=(
        "F-CS17: legacy DockerRuntime (CLI/TUI in docker) is slated "
        "for deletion. The new ContainerRuntime (SDK in docker) is "
        "tested in tests/scitex_agent_container/runtimes/test_container.py. "
        "DockerRuntime + this test go in F-CS17 stage 3."
    )
)
@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
def test_start_and_stop_newbie_docker_agent(request, tmp_path, test_image):
    """Start the newbie-docker agent, verify is_running, then stop it."""
    from scitex_agent_container.runtimes.docker import DockerRuntime

    agent_name = f"newbie-docker-test-{os.getpid()}"
    config = _load_newbie_config_with_override(tmp_path, agent_name, image=test_image)
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
# c. `claude -p "hello"` inside the container (real LLM call)
# ---------------------------------------------------------------------------


def _extract_cache_creation_tokens(envelope: dict) -> int:
    """Pull cache_creation_input_tokens from a claude -p JSON envelope.

    Checks both top-level ``usage`` and ``modelUsage``. Returns 0 if absent.
    """
    if "usage" in envelope and isinstance(envelope["usage"], dict):
        v = envelope["usage"].get("cache_creation_input_tokens")
        if v is not None:
            return int(v)
    if "modelUsage" in envelope and isinstance(envelope["modelUsage"], dict):
        for _model, usage in envelope["modelUsage"].items():
            if isinstance(usage, dict):
                v = usage.get("cache_creation_input_tokens")
                if v is not None:
                    return int(v)
    return 0


@pytest.mark.docker_smoke
@pytest.mark.slow
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
@pytest.mark.skipif(
    not _CI_KEY_SET,
    reason="SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY not set",
)
def test_claude_p_hello_inside_container(request, test_image):
    """Real LLM call inside the newbie container must return a clean result."""
    container = f"newbie-docker-exec-{os.getpid()}"

    def _teardown():
        _docker("rm", "-f", container)

    request.addfinalizer(_teardown)

    _run_bare_container(test_image, container)

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
    assert envelope.get("type") == "result", f"envelope: {envelope}"
    assert envelope.get("is_error") is False, f"envelope: {envelope}"


# ---------------------------------------------------------------------------
# cc. Isolation comparison: newbie should load far less context than host
# ---------------------------------------------------------------------------


@pytest.mark.docker_smoke
@pytest.mark.slow
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
@pytest.mark.skipif(
    not _CI_KEY_SET,
    reason="SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY not set",
)
def test_newbie_has_far_smaller_cache_than_host(request, test_image):
    """Newbie container should cache-create <<< host (isolation evidence).

    Host baseline observed earlier ~36305 tokens (loaded skills/CLAUDE.md/
    memory). A clean container should be well under 1/3 of that.
    """
    host_claude = shutil.which("claude")
    if not host_claude:
        pytest.skip("host claude CLI not on PATH for baseline comparison")

    # --- Host run (uses whatever context the host loads by default) ---
    ci_key = os.environ["SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY"]
    host_env = {**os.environ, "ANTHROPIC_API_KEY": ci_key}
    host_res = subprocess.run(
        [
            host_claude,
            "-p",
            "hello",
            "--output-format",
            "json",
            "--model",
            "claude-haiku-4-5",
        ],
        capture_output=True,
        text=True,
        env=host_env,
        timeout=120,
    )
    assert host_res.returncode == 0, f"host claude failed: {host_res.stderr}"
    host_env_json = json.loads(host_res.stdout)
    host_cache = _extract_cache_creation_tokens(host_env_json)

    # --- Container run ---
    container = f"newbie-cache-cmp-{os.getpid()}"

    def _teardown():
        _docker("rm", "-f", container)

    request.addfinalizer(_teardown)
    _run_bare_container(test_image, container)

    c_res = _docker(
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
    assert c_res.returncode == 0, f"container claude failed: {c_res.stderr}"
    newbie_env_json = json.loads(c_res.stdout)
    newbie_cache = _extract_cache_creation_tokens(newbie_env_json)

    print(
        f"\n[isolation] host cache_creation_input_tokens={host_cache} "
        f"newbie={newbie_cache}"
    )

    # Threshold: newbie <= host / 3. Host baseline ~36305 observed earlier;
    # a fresh container has no skills/CLAUDE.md/memory so cache creation
    # should be a tiny fraction of the host's.
    assert newbie_cache <= max(host_cache // 3, 1), (
        f"expected newbie_cache ({newbie_cache}) <= host_cache/3 "
        f"({host_cache // 3}); host={host_cache}"
    )


# ---------------------------------------------------------------------------
# d. Filesystem isolation: no host claude files in container
# ---------------------------------------------------------------------------


@pytest.mark.docker_smoke
@pytest.mark.skipif(not _DOCKER_OK, reason="docker unavailable (CLI or daemon)")
def test_newbie_container_has_no_host_claude_files(request, test_image):
    """Container must not carry host .claude skills/MCP/memory.

    Fast (no LLM call). Checks that /home/agent/.claude and /root/.claude
    either do not exist or are empty, and /workspace does not contain host
    skill files.
    """
    container = f"newbie-fs-iso-{os.getpid()}"

    def _teardown():
        _docker("rm", "-f", container)

    request.addfinalizer(_teardown)
    _run_bare_container(test_image, container)

    for p in ("/home/agent/.claude", "/root/.claude"):
        res = _docker("exec", container, "sh", "-c", f"ls -A {p} 2>/dev/null || true")
        # Either the dir doesn't exist (empty stdout) or it's empty.
        leftover = res.stdout.strip()
        assert not leftover, (
            f"{p} contains host-like contents inside container:\n{leftover}"
        )

    # /workspace should be empty or at least not have any skill markers.
    res = _docker(
        "exec",
        container,
        "sh",
        "-c",
        "ls -A /workspace 2>/dev/null || true",
    )
    ws = res.stdout.strip().splitlines()
    bad = [
        entry
        for entry in ws
        if entry.lower() in {"skills", "claude.md", "mcp.json", "memory"}
    ]
    assert not bad, f"/workspace leaks host skill files: {bad}"


# ---------------------------------------------------------------------------
# e. Fast unit tests for opt-in ~/.claude auto-mount
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
            "runtime": "docker",
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
