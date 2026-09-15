"""Tests for the launch-time drift guard wired into ``agent_start``.

PA-306: no mocks. The spec lives inside a REAL git repo (``git init``
in tmp_path); a real hand-rolled fake runtime/handover capture whether
``start`` was reached. HOME + SCITEX_DIR are redirected into tmp_path so
the drift fetch-cache and Path.home() never touch the developer's home.

Covers the single invariant: only a source verified CURRENT may launch.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._drift import SpecSourceDriftError
from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


class _FakeRuntime:
    """Real runtime surface; records whether start() was reached."""

    def __init__(self) -> None:
        self.started: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return False

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.started.append(config)
        return True

    def stop(self, config: AgentConfig) -> None:  # pragma: no cover - unused here
        pass


class _FakeHandover:
    """Real handover surface; no-op for the four module callables."""

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    prev_home = os.environ.get("HOME")
    prev_dir = os.environ.get("SCITEX_DIR")
    os.environ["HOME"] = str(home)
    os.environ["SCITEX_DIR"] = str(home / ".scitex")
    try:
        yield
    finally:
        for key, prev in (("HOME", prev_home), ("SCITEX_DIR", prev_dir)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def _make_spec_repo(tmp_path: Path, *, drifted: bool) -> Path:
    """Create a real git clone holding the agent spec; optionally BEHIND.

    Returns the spec.yaml path. The spec is health-disabled and uses the
    apptainer runtime; the injected fake runtime handles the launch.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    work = tmp_path / "specsrc"
    subprocess.run(
        ["git", "clone", str(remote), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-b", "develop")
    agent_dir = work / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path / 'work'}\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  health:\n"
            "    enabled: false\n"
            "    interval: 60\n"
        )
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "develop")
    if drifted:
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", str(remote), str(other)], check=True, capture_output=True
        )
        _git(other, "config", "user.email", "t@example.com")
        _git(other, "config", "user.name", "Test")
        _git(other, "checkout", "develop")
        (other / "x.txt").write_text("remote")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "remote work")
        _git(other, "push")
    return spec


def _start(spec: Path, registry: Registry, runtime: _FakeRuntime, **kw):
    return agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
        sleep_fn=lambda _s: None,
        **kw,
    )


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


# ---------------------------------------------------------------------------
# default lenient vs strict block
# ---------------------------------------------------------------------------


def test_stale_source_blocks_by_default(tmp_path, registry):
    # Arrange — the flip: no flag at all, and a BEHIND source refuses.
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    ctx = pytest.raises(SpecSourceDriftError)
    # Assert
    with ctx:
        _start(spec, registry, runtime)


def test_stale_source_does_not_reach_runtime_by_default(tmp_path, registry):
    # Arrange
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    try:
        _start(spec, registry, runtime)
    except SpecSourceDriftError:
        pass
    # Assert — start() never ran.
    assert runtime.started == []


def test_stale_source_banner_says_error_not_warning(tmp_path, registry, capsys):
    # Arrange — a refusal that still reads "WARNING" trains the wrong reflex.
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    try:
        _start(spec, registry, runtime)
    except SpecSourceDriftError:
        pass
    # Assert
    assert "sac-drift ERROR" in capsys.readouterr().err


def test_refusal_names_the_required_fix(tmp_path, registry, capsys):
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    try:
        _start(spec, registry, runtime)
    except SpecSourceDriftError:
        pass
    assert "synchronize the spec source and retry" in capsys.readouterr().err


def test_false_strict_compatibility_argument_is_rejected(tmp_path, registry):
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    with pytest.raises(TypeError):
        _start(spec, registry, runtime, strict_drift=False)


def test_unpushed_local_commits_refuse(tmp_path, registry):
    spec = _make_spec_repo(tmp_path, drifted=False)
    (spec.parent / "local.txt").write_text("local only")
    _git(spec.parent.parent.parent, "add", "-A")
    _git(spec.parent.parent.parent, "commit", "-m", "local work")
    runtime = _FakeRuntime()
    with pytest.raises(SpecSourceDriftError):
        _start(spec, registry, runtime)


def test_unpushed_local_commits_report_error(tmp_path, registry, capsys):
    spec = _make_spec_repo(tmp_path, drifted=False)
    (spec.parent / "local.txt").write_text("local only")
    _git(spec.parent.parent.parent, "add", "-A")
    _git(spec.parent.parent.parent, "commit", "-m", "local work")
    runtime = _FakeRuntime()
    with pytest.raises(SpecSourceDriftError):
        _start(spec, registry, runtime)
    assert "sac-drift ERROR" in capsys.readouterr().err


def test_clean_source_starts_normally(pg_schema: str, tmp_path, registry):
    # Arrange
    spec = _make_spec_repo(tmp_path, drifted=False)
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime)
    # Assert
    assert len(runtime.started) == 1
