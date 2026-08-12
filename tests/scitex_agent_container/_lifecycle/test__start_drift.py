"""Tests for the launch-time drift guard wired into ``agent_start``.

PA-306: no mocks. The spec lives inside a REAL git repo (``git init``
in tmp_path); a real hand-rolled fake runtime/handover capture whether
``start`` was reached. HOME + SCITEX_DIR are redirected into tmp_path so
the drift fetch-cache and Path.home() never touch the developer's home.

Covers:
  * DEFAULT (2026-08-10 operator ruling) → a STALE source REFUSES to start,
    raising SpecSourceDriftError BEFORE the runtime is touched.
  * --allow-stale-spec / SAC_ALLOW_STALE_SPEC → starts anyway, loudly.
  * AHEAD (unpushed local commits) is NOT staleness → still starts by default.
  * env SAC_ALLOW_STALE_SPEC / legacy SAC_STRICT_DRIFT honoured by
    ``_resolve_strict_drift`` (an explicit arg wins over both).
  * a clean (current) source launches normally.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._drift import SpecSourceDriftError
from scitex_agent_container._lifecycle._start import _resolve_strict_drift, agent_start
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


def test_refusal_names_the_named_override(tmp_path, registry, capsys):
    # Arrange — one flag per condition; never a blanket --force.
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    try:
        _start(spec, registry, runtime)
    except SpecSourceDriftError:
        pass
    # Assert
    assert "--allow-stale-spec" in capsys.readouterr().err


def test_allow_stale_spec_starts_the_agent(tmp_path, registry):
    # Arrange — the escape hatch, passed the way --allow-stale-spec passes it.
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime, strict_drift=False)
    # Assert
    assert len(runtime.started) == 1


def test_allow_stale_spec_env_starts_the_agent(tmp_path, registry, env_save_restore):
    # Arrange — same override, env transport (what the parallel path inherits).
    env_save_restore.set("SAC_ALLOW_STALE_SPEC", "1")
    spec = _make_spec_repo(tmp_path, drifted=True)
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime)
    # Assert
    assert len(runtime.started) == 1


def test_unpushed_local_commits_still_start(tmp_path, registry):
    # Arrange — AHEAD is not staleness: the spec here IS the newest that
    # exists. Hosts like spartan legitimately carry local commits.
    spec = _make_spec_repo(tmp_path, drifted=False)
    (spec.parent / "local.txt").write_text("local only")
    _git(spec.parent.parent.parent, "add", "-A")
    _git(spec.parent.parent.parent, "commit", "-m", "local work")
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime)
    # Assert
    assert len(runtime.started) == 1


def test_unpushed_local_commits_still_warn(tmp_path, registry, capsys):
    # Arrange — not refusing is not the same as staying quiet.
    spec = _make_spec_repo(tmp_path, drifted=False)
    (spec.parent / "local.txt").write_text("local only")
    _git(spec.parent.parent.parent, "add", "-A")
    _git(spec.parent.parent.parent, "commit", "-m", "local work")
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime)
    # Assert
    assert "sac-drift WARNING" in capsys.readouterr().err


def test_clean_source_starts_normally(tmp_path, registry):
    # Arrange
    spec = _make_spec_repo(tmp_path, drifted=False)
    runtime = _FakeRuntime()
    # Act
    _start(spec, registry, runtime, strict_drift=True)
    # Assert
    assert len(runtime.started) == 1


# ---------------------------------------------------------------------------
# _resolve_strict_drift — arg-wins / env-fallback
# ---------------------------------------------------------------------------


def test_explicit_true_arg_wins_over_env(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_ALLOW_STALE_SPEC", "1")
    # Act
    resolved = _resolve_strict_drift(True)
    # Assert
    assert resolved is True


def test_explicit_false_arg_wins_over_env(env_save_restore):
    # Arrange — what --allow-stale-spec passes; a stale export must not undo it.
    env_save_restore.set("SAC_STRICT_DRIFT", "1")
    # Act
    resolved = _resolve_strict_drift(False)
    # Assert
    assert resolved is False


def test_allow_stale_env_disables_strict(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_ALLOW_STALE_SPEC", "1")
    # Act
    resolved = _resolve_strict_drift(None)
    # Assert
    assert resolved is False


def test_legacy_strict_drift_zero_still_disables_strict(env_save_restore):
    # Arrange — an existing SAC_STRICT_DRIFT=0 meant "do not block me"; the
    # flipped default must not silently turn that export into a no-op.
    env_save_restore.delete("SAC_ALLOW_STALE_SPEC")
    env_save_restore.set("SAC_STRICT_DRIFT", "0")
    # Act
    resolved = _resolve_strict_drift(None)
    # Assert
    assert resolved is False


def test_env_unset_now_defaults_to_strict(env_save_restore):
    # Arrange — the operator ruling, pinned.
    env_save_restore.delete("SAC_STRICT_DRIFT")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_STRICT_DRIFT")
    env_save_restore.delete("SAC_ALLOW_STALE_SPEC")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_ALLOW_STALE_SPEC")
    # Act
    resolved = _resolve_strict_drift(None)
    # Assert
    assert resolved is True
