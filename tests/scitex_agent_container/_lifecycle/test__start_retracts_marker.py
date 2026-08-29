"""Tests for the P3 retraction call site: the ALIVE no-op in ``agent_start``.

Exactly ONE place retracts a marker — the already-running no-op reached
only when ``resolve_start_verdict`` yields ``ALIVE`` (positive liveness
evidence). Every other exit from ``agent_start`` — dry-run, a failed
``runtime.start()``, and a launch that merely RETURNED truthy without any
positive evidence of life — must leave an existing marker untouched.
``runtimes/_apptainer_runtime.py`` and ``runtimes/tui_session.py`` both
``return True`` on paths that observed nothing (a bare ``Popen``, a tmux
session NAME existing), so retracting there would delete a true stillborn
record on exactly the path the marker exists for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._lifecycle._start_outcome import (
    KIND_ALREADY_RUNNING,
    outcome_kind,
)
from scitex_agent_container._lifecycle._startup_failed import (
    MARKER_FILENAME,
    RETRACTED_MARKER_FILENAME,
    write_marker,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig

# ---------------------------------------------------------------------------
# Fixtures — real HOME/runtime-dir redirection, real hand-rolled fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_runtime_dir(tmp_path: Path, env_save_restore) -> Path:
    """Redirect $HOME + the runtime-dir env so ``state_dir_for`` (used by
    both the test's marker seeding and ``agent_start``'s internal
    ``retract_marker_for``) resolves to the SAME tmp_path root.
    """
    import importlib

    import scitex_agent_container._runners._session_state as ss

    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    importlib.reload(ss)
    env_save_restore.reload_after_restore(ss)
    yield runtime


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


def _write_spec(tmp_path: Path, *, name: str = "alpha") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  hooks:\n    pre_start: []\n    post_start: []\n"
        "    pre_stop: []\n    post_stop: []\n"
    )
    from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

    spec = agent_dir / "spec.yaml"
    spec.write_text(explicitize_yaml(body))
    return spec


class FakeRuntime:
    def __init__(self, *, running: bool = False, start_result: bool = True) -> None:
        self.running = running
        self.start_result = start_result
        self.start_calls: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return self.running

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.start_calls.append(config)
        return self.start_result

    def stop(self, config: AgentConfig) -> None:
        pass

    def logs(self, config: AgentConfig, lines: int) -> str:
        return ""


class FakeThread:
    """Hand-rolled stand-in for ``threading.Thread`` that NEVER runs.

    ``_write_spec`` enables ``health``, so ``agent_start`` reaches
    ``_start_supervision``, which does::

        thread = thread_factory(target=health_monitor, ..., daemon=True)
        thread.start()

    With the real ``threading.Thread`` that daemon thread OUTLIVES the test
    and keeps looping ``health_monitor -> restart_and_record ->
    write_birth_certificate`` for the rest of the worker process. Its
    ``logger.error`` then lands in whatever capture buffer happens to be open
    -- an unrelated test's ``CliRunner`` -- ahead of that command's own
    output, which is how a PASSING test here turns a stranger's assertion red
    three files later. Measured on develop 2026-08-21: 31 stray certificates
    in one run, plus ``ValueError: I/O operation on closed file`` from writing
    into a stream pytest had already torn down.

    Recording ``start()`` rather than swallowing it keeps the seam honest: a
    test that wants to assert the monitor was launched still can. Same shape
    as ``_RecordingThread`` in ``test__start_supervision.py`` and
    ``_CapturingThread`` in ``test__instances_auto_grant.py``.

    PA-306: a hand-written stand-in, not a mock.
    """

    def __init__(self, *, target=None, args=(), daemon=False, **_kw) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class FakeHandover:
    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "fake-uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def push_pre_stop_snapshot(self, config: AgentConfig, payload=None) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


def _no_sleep(_seconds: float) -> None:
    return None


def _seed_marker(runtime_dir: Path) -> None:
    write_marker(
        runtime_dir,
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="FATAL: container creation failed: mount source /work/x doesn't exist",
    )


# ---------------------------------------------------------------------------
# The ALIVE no-op retracts an existing marker
# ---------------------------------------------------------------------------


def test_the_already_running_noop_returns_the_already_running_outcome(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    ok = agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
    )
    # Assert
    assert outcome_kind(ok) == KIND_ALREADY_RUNNING


def test_the_already_running_noop_retracts_an_existing_marker(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    _seed_marker(isolated_runtime_dir / "alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
    )
    # Assert
    assert not (isolated_runtime_dir / "alpha" / MARKER_FILENAME).exists()


def test_the_already_running_noop_leaves_the_retracted_copy(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    _seed_marker(isolated_runtime_dir / "alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
    )
    # Assert
    assert (isolated_runtime_dir / "alpha" / RETRACTED_MARKER_FILENAME).is_file()


def test_the_already_running_noop_with_no_marker_does_not_raise(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange — no marker on disk at all; retract_marker_for must be a
    # silent no-op, never an exception that would break a real no-op start.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    ok = agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
    )
    # Assert
    assert outcome_kind(ok) == KIND_ALREADY_RUNNING


# ---------------------------------------------------------------------------
# A launch that merely RETURNED (no positive liveness evidence) does NOT
# retract — pins the design decision: runtime.start() truthy is not
# evidence the agent came up.
# ---------------------------------------------------------------------------


def test_a_launch_that_merely_returned_keeps_the_marker(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange — nothing vouches for liveness (no registry row, no
    # liveness_verifier): resolve_start_verdict yields UNKNOWN, so
    # agent_start proceeds to a real start. The fake runtime "returns
    # True" without having observed anything, exactly like
    # ``_apptainer_runtime.py``'s bare Popen.
    spec = _write_spec(tmp_path)
    _seed_marker(isolated_runtime_dir / "alpha")
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
    )
    # Assert
    assert (isolated_runtime_dir / "alpha" / MARKER_FILENAME).is_file()


# ---------------------------------------------------------------------------
# A dry run does not retract
# ---------------------------------------------------------------------------


def test_a_dry_run_on_an_alive_agent_keeps_the_marker(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange — ALIVE verdict (registry + running + verifier), but
    # dry_run=True takes the ``elif dry_run: pass`` branch, never the
    # no-op else branch that retracts.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    _seed_marker(isolated_runtime_dir / "alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
        dry_run=True,
    )
    # Assert
    assert (isolated_runtime_dir / "alpha" / MARKER_FILENAME).is_file()


# ---------------------------------------------------------------------------
# A failed start does not retract
# ---------------------------------------------------------------------------


def test_a_failed_start_keeps_the_marker(
    pg_schema: str,
    tmp_path: Path, isolated_runtime_dir: Path, registry: Registry
) -> None:
    # Arrange — runtime.start() returns False -> raise_start_failure raises
    # before any retraction could be reached on this path either way.
    spec = _write_spec(tmp_path)
    _seed_marker(isolated_runtime_dir / "alpha")
    runtime = FakeRuntime(running=False, start_result=False)
    # Act
    try:
        agent_start(
            str(spec),
            thread_factory=FakeThread,
            registry=registry,
            runtime_factory=lambda _c: runtime,
            handover_mod=FakeHandover(),
            sleep_fn=_no_sleep,
        )
    except RuntimeError:
        pass
    # Assert
    assert (isolated_runtime_dir / "alpha" / MARKER_FILENAME).is_file()
