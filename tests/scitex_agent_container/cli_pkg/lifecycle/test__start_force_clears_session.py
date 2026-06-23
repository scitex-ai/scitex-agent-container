"""Tests for the ``--force`` session_id wipe seam.

``sac agents start --force <name>`` must clear any persisted SDK
``session_id`` resume marker so the next runtime.start cannot silently
re-resume an aged-out conversation (server-side TTL is finite; a stale
id surfaces as ``ProcessError: Command failed with exit code 1`` ~90s
into the first turn).

The seam is :func:`scitex_agent_container._lifecycle.lifecycle.agent_start`
— that is the entry point both the CLI (``sac agents start``) and the
MCP/programmatic callers delegate to. Driving the test through
``agent_start`` covers every caller in one pass.

These tests follow project conventions:

* No ``monkeypatch`` / ``mocker`` (STX-NM002). HOME and the runtime
  root env var are flipped via a ``yield``-based env-var fixture.
* One assert per test (STX-TQ007), AAA markers.
* Real collaborators throughout: real :class:`Registry`, real on-disk
  YAML loaded by the real ``load_config``, hand-rolled ``FakeRuntime``
  / ``FakeHandover`` mirroring the production call surface.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig

# ---------------------------------------------------------------------------
# Fixtures — real env, real Registry, no monkeypatch
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime_root(tmp_path: Path) -> Iterator[Path]:
    """Point sac's runtime root at ``tmp_path/runtime`` for this test.

    The lifecycle code reads ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` at
    call time (not import time), so an env-var swap is the honest
    injection seam — no monkeypatch needed.
    """
    root = tmp_path / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    prev = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(root)
    try:
        yield root
    finally:
        if prev is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = prev


@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect HOME for any code path that still falls back to ``Path.home()``."""
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


# ---------------------------------------------------------------------------
# Real hand-rolled fakes (no unittest.mock) — same surface as
# test_lifecycle.py's FakeRuntime / FakeHandover.
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Real collaborator implementing the runtime surface lifecycle uses."""

    def __init__(
        self,
        *,
        running: bool = False,
        start_result: bool = True,
    ) -> None:
        self.running = running
        self.start_result = start_result
        self.start_calls: list[AgentConfig] = []
        self.stop_calls: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return self.running

    def start(self, config: AgentConfig, **_kwargs: Any) -> bool:
        self.start_calls.append(config)
        return self.start_result

    def stop(self, config: AgentConfig) -> None:
        self.stop_calls.append(config)

    def logs(self, config: AgentConfig, lines: int) -> str:
        return ""


class FakeHandover:
    """Real collaborator implementing the handover module surface."""

    def __init__(self) -> None:
        self.ensure_calls: list[AgentConfig] = []
        self.hydrate_calls: list[AgentConfig] = []

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        self.ensure_calls.append(config)
        return "fake-uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        self.hydrate_calls.append(config)
        return True

    def push_pre_stop_snapshot(
        self, config: AgentConfig, payload: dict | None = None
    ) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        return None


def _no_sleep(_seconds: float) -> None:
    return None


def _write_spec(workdir_root: Path, *, name: str = "alpha") -> Path:
    """Write a minimal valid v3 YAML at ``<workdir_root>/<name>/spec.yaml``."""
    agent_dir = workdir_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: local\n"
        f"  workdir: {workdir_root / 'work'}\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  hooks:\n"
        "    pre_start: []\n"
        "    post_start: []\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
    )
    spec = agent_dir / "spec.yaml"
    spec.write_text(body)
    return spec


def _seed_session_id(runtime_root: Path, name: str, sid: str) -> Path:
    """Plant a persisted session_id file under the test runtime root."""
    state_dir = runtime_root / name
    state_dir.mkdir(parents=True, exist_ok=True)
    sid_path = state_dir / "session_id"
    sid_path.write_text(sid, encoding="utf-8")
    return sid_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_force_removes_persisted_session_id_file(
    tmp_path: Path,
    runtime_root: Path,
    isolated_home: Path,
    registry: Registry,
) -> None:
    # Arrange
    spec = _write_spec(tmp_path, name="alpha")
    sid_path = _seed_session_id(
        runtime_root, "alpha", "17f7cf41-94f5-41d1-a20d-535a22b254a1"
    )
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
    )
    # Assert
    assert not sid_path.exists()


def test_force_leaves_other_runtime_state_alone(
    tmp_path: Path,
    runtime_root: Path,
    isolated_home: Path,
    registry: Registry,
) -> None:
    # Arrange: seed session_id plus unrelated runtime files that --force
    # MUST NOT touch (heartbeat.json, stdout.log, quota.json, …).
    spec = _write_spec(tmp_path, name="alpha")
    _seed_session_id(runtime_root, "alpha", "stale-sid")
    state_dir = runtime_root / "alpha"
    other_files = {
        state_dir / "heartbeat.json": '{"ts":1,"pid":1,"state":"idle"}',
        state_dir / "stdout.log": "previous run output\n",
        state_dir / "quota.json": '{"input_tokens":42}',
    }
    for path, body in other_files.items():
        path.write_text(body, encoding="utf-8")
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
    )
    # Assert: every untouched file still exists with original contents.
    assert all(
        p.exists() and p.read_text(encoding="utf-8") == body
        for p, body in other_files.items()
    )


def test_no_force_leaves_session_id(
    tmp_path: Path,
    runtime_root: Path,
    isolated_home: Path,
    registry: Registry,
) -> None:
    # Arrange: persisted session_id present, agent NOT running so the
    # default (no --force) launch path is exercised end-to-end.
    spec = _write_spec(tmp_path, name="alpha")
    sid_path = _seed_session_id(runtime_root, "alpha", "preserve-me")
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=False,
    )
    # Assert
    assert sid_path.read_text(encoding="utf-8") == "preserve-me"


def test_missing_session_id_file_under_force_is_no_op(
    tmp_path: Path,
    runtime_root: Path,
    isolated_home: Path,
    registry: Registry,
) -> None:
    # Arrange: first-ever start — no session_id file on disk.
    spec = _write_spec(tmp_path, name="alpha")
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
    )
    # Assert: agent_start completed without raising; no file means no file.
    assert ok is True
