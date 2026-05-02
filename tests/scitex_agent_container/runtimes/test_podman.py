"""Tests for ``runtimes/podman.py``.

PodmanRuntime is a thin DockerRuntime subclass that swaps the binary
name. The DockerRuntime methods themselves are exercised in the
integration suite; here we just confirm the override is wired
correctly so that every command this adapter shells out to ends up
calling ``podman`` instead of ``docker``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scitex_agent_container.runtimes.docker import DockerRuntime
from scitex_agent_container.runtimes.podman import PodmanRuntime


class _FakeContainer:
    runtime = "none"
    network = "none"
    image = "test:latest"
    volumes: list[str] = []
    mount_host_claude = False


class _FakeClaude:
    flags: list[str] = []
    channels: list[str] = []
    session = "new"


class _FakeConfig:
    name = "alpha"
    expanded_workdir = "/tmp/alpha"
    container = _FakeContainer()
    claude = _FakeClaude()
    env: dict = {}
    model = "claude-haiku-4-5"


def test_subclass_relationship() -> None:
    assert issubclass(PodmanRuntime, DockerRuntime)
    assert PodmanRuntime.BIN == "podman"
    assert DockerRuntime.BIN == "docker"


def test_stop_invokes_podman_not_docker() -> None:
    rt = PodmanRuntime()
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rt.stop(_FakeConfig())  # type: ignore[arg-type]
    assert run.call_count == 2
    for call in run.call_args_list:
        argv = call.args[0]
        assert argv[0] == "podman", f"expected podman, got {argv[0]!r}"


def test_is_running_invokes_podman_ps() -> None:
    rt = PodmanRuntime()
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="sac-alpha\n", stderr="")
        assert rt.is_running(_FakeConfig()) is True  # type: ignore[arg-type]
    argv = run.call_args.args[0]
    assert argv[:2] == ["podman", "ps"]


def test_logs_invokes_podman_logs() -> None:
    rt = PodmanRuntime()
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="hi", stderr="")
        out = rt.logs(_FakeConfig(), lines=10)  # type: ignore[arg-type]
    assert out == "hi"
    argv = run.call_args.args[0]
    assert argv[:2] == ["podman", "logs"]
    assert "10" in argv


def test_build_image_classmethod_uses_podman() -> None:
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        ok = PodmanRuntime.build_image(image="x:1", context="ctx")
    assert ok is True
    argv = run.call_args.args[0]
    assert argv[:2] == ["podman", "build"]
    # Docker.build_image (classmethod parent) keeps using "docker".
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        DockerRuntime.build_image(image="x:1", context="ctx")
    assert run.call_args.args[0][0] == "docker"
