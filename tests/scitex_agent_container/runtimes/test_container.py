"""Tests for ``runtimes.container.ContainerRuntime`` (F-CS16 phase 2b).

Two layers:

  1. ``build_run_argv`` — pure function; render the right ``docker
     run`` flags from an AgentConfig. No subprocess work; we exercise
     workdir / state mount, env vars, env-files, --publish for a2a,
     image fallback, runner argv default + override.

  2. ``start`` / ``stop`` / ``is_running`` / ``logs`` — wired to
     subprocess. Mocked via ``monkeypatch.setattr(subprocess, "run")``
     so the tests stay hermetic; the assertions cover the argv shape,
     the container_id sidecar lifecycle, and the read paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.container import (
    CONTAINER_ID_FILE,
    DEFAULT_IMAGE,
    ContainerRuntime,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the runner's state-dir root so each test stays sandboxed."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    # Reload the _session_state module so it picks up the env var. Some
    # _runners.claude_session attributes cache the root; reload re-derives.
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    return root


def _config(workdir: Path, **kw) -> AgentConfig:
    """Minimal AgentConfig for the dispatch tests."""
    return AgentConfig(
        name=kw.pop("name", "alpha"),
        runtime=kw.pop("runtime", "docker"),
        workdir=str(workdir),
        **kw,
    )


# ---------------------------------------------------------------------------
# build_run_argv — pure
# ---------------------------------------------------------------------------


def test_build_run_argv_emits_default_image_and_basic_mounts(tmp_path: Path):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    state = tmp_path / "state"
    state.mkdir()

    argv = rt.build_run_argv(cfg, state_dir=state)

    # Engine + run + detach flags first.
    assert argv[:4] == ["docker", "run", "--detach", "--rm"]
    # Default image when spec.image is empty.
    assert DEFAULT_IMAGE in argv
    # Workdir bind mount points at /work; state bind mount at /state.
    assert f"type=bind,src={tmp_path / 'wd'},dst=/work" in argv
    assert f"type=bind,src={state},dst=/state" in argv
    # SCITEX_AGENT_CONTAINER_STATE_DB env wired.
    assert "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db" in argv


def test_build_run_argv_uses_spec_image_when_set(tmp_path: Path):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path, image="clew-paper:capsule-01")
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "clew-paper:capsule-01" in argv
    assert DEFAULT_IMAGE not in argv


def test_build_run_argv_passes_env_dict_as_separate_flags(tmp_path: Path):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path, env={"CAPSULE_ID": "01", "PROJECT": "clew"})
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    # Each KEY=VAL is its own --env arg.
    assert "CAPSULE_ID=01" in argv
    assert "PROJECT=clew" in argv
    # Both are preceded by --env (not collapsed).
    assert argv.count("--env") >= 2


def test_build_run_argv_threads_env_files(tmp_path: Path):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path, env_files=[".envrc", "secrets.env"])
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    # --env-file appears once per file.
    idxs = [i for i, a in enumerate(argv) if a == "--env-file"]
    assert len(idxs) == 2
    assert argv[idxs[0] + 1] == ".envrc"
    assert argv[idxs[1] + 1] == "secrets.env"


def test_build_run_argv_publishes_a2a_to_localhost(tmp_path: Path):
    """a2a.port -> --publish 127.0.0.1:<port>:<port>.

    Phase 2b's ContainerRuntime reads the port via
    ``getattr(config.a2a, 'port', None)``. AgentConfig doesn't carry
    an ``a2a`` field today (the port is read inline from the raw
    yaml spec by other call sites — see runtimes.claude_session.
    _read_a2a_endpoint). Stub the attribute on the instance so we
    exercise the publish-flag branch without prematurely shaping
    AgentConfig — the proper ``A2ASpec`` field lands in F-CS16
    phase 2c when all agentic-sugar mounts get formalised.
    """
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path)
    object.__setattr__(cfg, "a2a", type("A", (), {"port": 8950})())

    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "--publish" in argv
    pub = argv[argv.index("--publish") + 1]
    assert pub == "127.0.0.1:8950:8950"
    # Runner argv also gets the --a2a-port pair.
    assert "--a2a-port" in argv
    assert argv[argv.index("--a2a-port") + 1] == "8950"


def test_build_run_argv_runner_argv_override(tmp_path: Path):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path, name="beta")
    custom = ["--name", "beta", "--mission", "smoke"]
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, runner_argv=custom)
    # The override appears at the tail (after the image).
    assert argv[-len(custom) :] == custom
    # Default --a2a-port pair must not creep in when caller supplies argv.
    assert "--mission" in argv


def test_build_run_argv_forwards_startup_command_as_mission(tmp_path: Path):
    """startup_commands[0].command becomes --mission (+ --print-stream)."""
    from scitex_agent_container.config._types import StartupCommand

    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path,
        startup_commands=[StartupCommand(command="run smoke")],
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "--mission" in argv
    assert argv[argv.index("--mission") + 1] == "run smoke"
    assert "--print-stream" in argv


def test_build_run_argv_forwards_autonomous_block(tmp_path: Path):
    """spec.autonomous flags propagate as --autonomous-* CLI args (F-CS3 phase 2)."""
    from scitex_agent_container.config._types import AutonomousSpec, StartupCommand

    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path,
        startup_commands=[StartupCommand(command="seed")],
        autonomous=AutonomousSpec(
            enabled=True,
            drive_until="ALL DONE",
            max_turns=12,
            kick_text="keep going",
        ),
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "--autonomous-enabled" in argv
    assert argv[argv.index("--autonomous-drive-until") + 1] == "ALL DONE"
    assert argv[argv.index("--autonomous-max-turns") + 1] == "12"
    assert argv[argv.index("--autonomous-kick-text") + 1] == "keep going"


def test_build_run_argv_skips_autonomous_when_disabled(tmp_path: Path):
    from scitex_agent_container.config._types import AutonomousSpec, StartupCommand

    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path,
        startup_commands=[StartupCommand(command="seed")],
        autonomous=AutonomousSpec(enabled=False),
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "--autonomous-enabled" not in argv


def test_build_run_argv_works_for_podman(tmp_path: Path):
    rt = ContainerRuntime("podman")
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert argv[0] == "podman"


def test_build_run_argv_rejects_unsupported_engine():
    with pytest.raises(ValueError, match="apptainer"):
        ContainerRuntime("apptainer")


# ---------------------------------------------------------------------------
# start / stop / is_running / logs — subprocess-mocked
# ---------------------------------------------------------------------------


class _FakeRun:
    """Drop-in for ``subprocess.run`` that records argv + replays canned output."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._next: list[subprocess.CompletedProcess] = []

    def queue(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self._next.append(
            subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout, stderr=stderr
            )
        )

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if self._next:
            r = self._next.pop(0)
            r.args = argv
            return r
        return subprocess.CompletedProcess(args=argv, returncode=0)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> _FakeRun:
    fr = _FakeRun()
    import scitex_agent_container.runtimes.container as cm

    monkeypatch.setattr(cm.subprocess, "run", fr)
    # shutil.which always returns a path so the engine-availability
    # gate doesn't short-circuit start().
    monkeypatch.setattr(cm.shutil, "which", lambda *_a, **_k: "/usr/bin/docker")
    return fr


def test_start_writes_container_id_sidecar(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    # F-CS16 phase 2d adds an `image inspect` precheck before run.
    fake_run.queue(returncode=0)  # image inspect: present
    fake_run.queue(returncode=0, stdout="abc123\n")  # run

    assert rt.start(cfg) is True
    # `docker run` is invoked at least once.
    assert any(c[0] == "docker" and c[1] == "run" for c in fake_run.calls)

    # container_id sidecar written.
    state_dir = rt._state_dir(cfg)
    cid_path = state_dir / CONTAINER_ID_FILE
    assert cid_path.is_file()
    assert cid_path.read_text() == "abc123"


def test_start_returns_false_on_engine_failure(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    fake_run.queue(returncode=0)  # image inspect: present
    fake_run.queue(returncode=1, stderr="error: launch failure")  # run
    assert rt.start(cfg) is False
    state_dir = rt._state_dir(cfg)
    assert not (state_dir / CONTAINER_ID_FILE).exists()


def test_stop_runs_docker_stop_and_clears_sidecar(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / CONTAINER_ID_FILE).write_text("abc123")
    fake_run.queue(returncode=0)

    assert rt.stop(cfg) is True
    assert fake_run.calls[-1][:3] == ["docker", "stop", "abc123"]
    assert not (state_dir / CONTAINER_ID_FILE).exists()


def test_stop_succeeds_when_no_container_id(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    # No sidecar at all → stop returns True (idempotent), no engine call.
    assert rt.stop(cfg) is True
    assert fake_run.calls == []


def test_is_running_true_when_inspect_returns_true(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / CONTAINER_ID_FILE).write_text("abc123")
    fake_run.queue(returncode=0, stdout="true\n")

    assert rt.is_running(cfg) is True
    assert fake_run.calls[-1][:2] == ["docker", "inspect"]


def test_is_running_false_when_inspect_returns_false(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / CONTAINER_ID_FILE).write_text("abc123")
    fake_run.queue(returncode=0, stdout="false\n")
    assert rt.is_running(cfg) is False


def test_is_running_false_when_no_sidecar(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    assert rt.is_running(cfg) is False
    assert fake_run.calls == []


def test_logs_calls_docker_logs_with_tail(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / CONTAINER_ID_FILE).write_text("abc123")
    fake_run.queue(returncode=0, stdout="line1\nline2\n", stderr="")
    out = rt.logs(cfg, lines=42)
    assert "line1" in out and "line2" in out
    cmd = fake_run.calls[-1]
    assert cmd[:2] == ["docker", "logs"]
    assert cmd[cmd.index("--tail") + 1] == "42"


def test_logs_empty_when_no_container_id(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    assert rt.logs(cfg) == ""


def test_dry_run_writes_argv_file(state_root: Path, tmp_path: Path, fake_run: _FakeRun):
    """--dry-run captures the would-run argv to the state dir."""
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    assert rt.start(cfg, dry_run=True) is True
    # No subprocess call when dry-running.
    assert fake_run.calls == []
    state_dir = rt._state_dir(cfg)
    argv_file = state_dir / "container_run.argv.txt"
    assert argv_file.is_file()
    assert "docker" in argv_file.read_text().splitlines()[0]


# ---------------------------------------------------------------------------
# F-CS16 phase 2d — auto-build + --user injection.
# ---------------------------------------------------------------------------


def test_build_run_argv_injects_user_flag(tmp_path: Path):
    """--user $(id -u):$(id -g) appears by default."""
    import os as _os

    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert "--user" in argv
    spec = argv[argv.index("--user") + 1]
    expected = f"{_os.getuid()}:{_os.getgid()}"
    assert spec == expected


def test_build_run_argv_user_override_via_env(tmp_path, monkeypatch):
    """SAC_USER overrides the auto-detected uid:gid."""
    monkeypatch.setenv("SAC_USER", "claude:claude")
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd")
    argv = rt.build_run_argv(cfg, state_dir=tmp_path)
    assert argv[argv.index("--user") + 1] == "claude:claude"


def test_ensure_image_present_skips_when_image_already_local(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    """When `docker image inspect <image>` exits 0, no build runs."""
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd", image="local-image:already-here")
    fake_run.queue(returncode=0)  # `docker image inspect` succeeds
    assert rt._ensure_image_present(cfg) is True
    cmds = [c[:3] for c in fake_run.calls]
    assert cmds == [["docker", "image", "inspect"]]


def test_ensure_image_present_auto_builds_from_dockerfile(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    """Image missing + dockerfile declared -> docker build runs."""
    dockerfile = tmp_path / "ctx" / "Dockerfile.x"
    dockerfile.parent.mkdir()
    dockerfile.write_text("FROM python:3.11-slim\n")

    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path / "wd",
        image="custom:x",
        dockerfile=str(dockerfile),
    )
    # First call: docker image inspect → 1 (missing).
    fake_run.queue(returncode=1)
    # Second call: docker build → 0.
    fake_run.queue(returncode=0)

    assert rt._ensure_image_present(cfg) is True
    assert fake_run.calls[0][:3] == ["docker", "image", "inspect"]
    build_cmd = fake_run.calls[1]
    assert build_cmd[:2] == ["docker", "build"]
    assert "-t" in build_cmd
    assert build_cmd[build_cmd.index("-t") + 1] == "custom:x"
    assert "-f" in build_cmd
    assert build_cmd[build_cmd.index("-f") + 1] == str(dockerfile.resolve())


def test_ensure_image_present_returns_false_when_no_dockerfile(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    """Image missing + no dockerfile -> caller surfaces a clean error."""
    rt = ContainerRuntime("docker")
    cfg = _config(tmp_path / "wd", image="custom:y")
    fake_run.queue(returncode=1)  # inspect: missing
    assert rt._ensure_image_present(cfg) is False
    # Only the inspect call ran — no build.
    assert len(fake_run.calls) == 1


def test_ensure_image_present_returns_false_when_build_fails(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path / "wd",
        image="custom:z",
        dockerfile=str(dockerfile),
    )
    fake_run.queue(returncode=1)  # inspect: missing
    fake_run.queue(returncode=2)  # build: fail
    assert rt._ensure_image_present(cfg) is False


def test_start_builds_missing_image_before_run(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    """End-to-end: image missing + dockerfile declared, start() chains
    inspect -> build -> run."""
    dockerfile = tmp_path / "ctx" / "Dockerfile.x"
    dockerfile.parent.mkdir()
    dockerfile.write_text("FROM python:3.11-slim\n")

    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path / "wd",
        image="custom:start",
        dockerfile=str(dockerfile),
    )
    fake_run.queue(returncode=1)  # inspect: missing
    fake_run.queue(returncode=0)  # build: ok
    fake_run.queue(returncode=0, stdout="cid-abc\n")  # run

    assert rt.start(cfg) is True
    cmd_kinds = [c[:2] for c in fake_run.calls]
    assert cmd_kinds == [
        ["docker", "image"],
        ["docker", "build"],
        ["docker", "run"],
    ]
    state_dir = rt._state_dir(cfg)
    assert (state_dir / CONTAINER_ID_FILE).read_text() == "cid-abc"


def test_start_skips_image_check_in_dry_run(
    state_root: Path, tmp_path: Path, fake_run: _FakeRun
):
    """Dry-run must NEVER trigger a real docker build/inspect."""
    rt = ContainerRuntime("docker")
    cfg = _config(
        tmp_path / "wd",
        image="custom:dry",
        dockerfile=str(tmp_path / "Dockerfile"),
    )
    assert rt.start(cfg, dry_run=True) is True
    assert fake_run.calls == []  # no docker invocation in dry-run
