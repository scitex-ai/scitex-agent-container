"""Tests for scitex_agent_container._state.snapshot._sidecars (no-mocks).

Threads are real ``threading.Thread`` instances driven by ``threading.Event``
so we deterministically control liveness without sleeps. Process-kind
sidecars are exercised by spawning a real child via ``subprocess.Popen``
(``sys.executable`` only — no shim needed; this module never invokes a
subprocess itself, it only probes PIDs via ``os.kill(pid, 0)``).
Registry state is isolated per test by snapshotting and restoring the
module-level ``_SIDECARS`` dict.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from scitex_agent_container._state.snapshot import _sidecars as mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_registry():
    """Snapshot/restore the module-level sidecar registry."""
    saved = {k: dict(v) for k, v in mod._SIDECARS.items()}
    mod._SIDECARS.clear()
    try:
        yield mod._SIDECARS
    finally:
        mod._SIDECARS.clear()
        mod._SIDECARS.update(saved)


@pytest.fixture
def stoppable_thread():
    """Real thread that exits when its event is set."""
    stop = threading.Event()
    th = threading.Thread(target=stop.wait, daemon=True)
    th.start()
    try:
        yield th, stop
    finally:
        stop.set()
        th.join(timeout=2.0)


@pytest.fixture
def live_child_process():
    """Real subprocess that idles until terminated."""
    proc = subprocess.Popen(  # stx-allow: test fixture spawns real process
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        yield proc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: test cleanup)
            proc.kill()
            proc.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# _project_agent_meta
# ---------------------------------------------------------------------------


def test_project_agent_meta_returns_none_when_missing():
    # Arrange
    meta = None
    # Act
    result = mod._project_agent_meta(meta)
    # Assert
    assert result is None


def test_project_agent_meta_returns_none_when_empty():
    # Arrange
    meta: dict = {}
    # Act
    result = mod._project_agent_meta(meta)
    # Assert
    assert result is None


def test_project_agent_meta_filters_to_allowed_keys():
    # Arrange
    raw = {
        "alive": True,
        "subagents": 2,
        "context_pct": 0.5,
        "current_tool": "Read",
        "last_activity": "2026-01-01",
        "model": "opus",
        "pane_tail": "tail-text",
        "pane_tail_block": "block",
        "recent_actions": [{"ts": 1, "preview": "x"}],
        "secret": "DROPME",
        "internal_only": 42,
    }
    # Act
    out = mod._project_agent_meta(raw)
    # Assert
    assert "secret" not in out and "internal_only" not in out


def test_project_agent_meta_preserves_value_for_known_key():
    # Arrange
    raw = {"alive": True, "extra": "drop"}
    # Act
    out = mod._project_agent_meta(raw)
    # Assert
    assert out == {"alive": True}


# ---------------------------------------------------------------------------
# register_sidecar
# ---------------------------------------------------------------------------


def test_register_sidecar_stores_thread_entry(clean_registry, stoppable_thread):
    # Arrange
    th, _stop = stoppable_thread
    # Act
    mod.register_sidecar("agent-a", "thread", "monitor", thread=th)
    # Assert
    assert clean_registry["agent-a"]["monitor"]["thread"] is th


def test_register_sidecar_stores_process_entry(clean_registry):
    # Arrange
    pid_value = 12345
    # Act
    mod.register_sidecar("agent-b", "process", "worker", pid=pid_value)
    # Assert
    assert clean_registry["agent-b"]["worker"]["pid"] == pid_value


def test_register_sidecar_supports_multiple_agents(clean_registry):
    # Arrange
    pids = (1, 2)
    # Act
    mod.register_sidecar("a1", "process", "x", pid=pids[0])
    mod.register_sidecar("a2", "process", "y", pid=pids[1])
    # Assert
    assert set(clean_registry.keys()) == {"a1", "a2"}


# ---------------------------------------------------------------------------
# _sidecar_alive — thread kind
# ---------------------------------------------------------------------------


def test_sidecar_alive_thread_true_when_running(stoppable_thread):
    # Arrange
    th, _stop = stoppable_thread
    info = {"kind": "thread", "thread": th}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is True


def test_sidecar_alive_thread_false_when_thread_missing():
    # Arrange
    info = {"kind": "thread", "thread": None}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is False


def test_sidecar_alive_thread_false_after_thread_exits():
    # Arrange
    stop = threading.Event()
    th = threading.Thread(target=stop.wait, daemon=True)
    th.start()
    stop.set()
    th.join(timeout=2.0)
    # Act
    result = mod._sidecar_alive({"kind": "thread", "thread": th})
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _sidecar_alive — process kind
# ---------------------------------------------------------------------------


def test_sidecar_alive_process_true_for_live_pid(live_child_process):
    # Arrange
    info = {"kind": "process", "pid": live_child_process.pid}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is True


def test_sidecar_alive_process_false_when_pid_missing():
    # Arrange
    info = {"kind": "process", "pid": None}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is False


def test_sidecar_alive_process_false_for_dead_pid():
    # Arrange: spawn and reap a real child so its PID is gone.
    proc = subprocess.Popen(  # stx-allow: test fixture spawns real process
        [sys.executable, "-c", "pass"]
    )
    proc.wait(timeout=5.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    # Act
    result = mod._sidecar_alive({"kind": "process", "pid": proc.pid})
    # Assert
    assert result is False


def test_sidecar_alive_process_returns_bool_for_pid_zero():
    # Arrange: PID 0 exercises the OSError/permission branch on Linux.
    info = {"kind": "process", "pid": 0}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert isinstance(result, bool)


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root can signal PID 1; need PermissionError path"
)
def test_sidecar_alive_process_true_for_permission_denied_pid():
    # Arrange: PID 1 (init) exists; non-root os.kill raises PermissionError
    # which production maps to "alive" (process exists, we just can't signal).
    info = {"kind": "process", "pid": 1}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is True


def test_sidecar_alive_process_false_for_oserror_negative_pid():
    # Arrange: a huge negative-style invalid pid forces OSError(EINVAL).
    # We use a value guaranteed not to be a live PID on Linux (max + 1).
    info = {"kind": "process", "pid": 2**31 - 1}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _sidecar_alive — unknown kind
# ---------------------------------------------------------------------------


def test_sidecar_alive_unknown_kind_returns_false():
    # Arrange
    info = {"kind": "mystery"}
    # Act
    result = mod._sidecar_alive(info)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _sidecars_payload
# ---------------------------------------------------------------------------


def test_sidecars_payload_empty_for_unknown_agent(clean_registry):
    # Arrange
    agent = "nobody"
    # Act
    payload = mod._sidecars_payload(agent)
    # Assert
    assert payload == {}


def test_sidecars_payload_reports_thread_kind(clean_registry, stoppable_thread):
    # Arrange
    th, _stop = stoppable_thread
    mod.register_sidecar("agent-x", "thread", "monitor", thread=th)
    # Act
    out = mod._sidecars_payload("agent-x")
    # Assert
    assert out == {"monitor": {"pid": None, "kind": "thread", "alive": True}}


def test_sidecars_payload_reports_process_kind(clean_registry, live_child_process):
    # Arrange
    mod.register_sidecar("agent-y", "process", "worker", pid=live_child_process.pid)
    # Act
    payload = mod._sidecars_payload("agent-y")
    # Assert
    assert payload["worker"]["alive"] is True


def test_sidecars_payload_includes_multiple_sidecars(clean_registry, stoppable_thread):
    # Arrange
    th, _stop = stoppable_thread
    mod.register_sidecar("agent-m", "thread", "one", thread=th)
    mod.register_sidecar("agent-m", "process", "two", pid=99999999)
    # Act
    payload = mod._sidecars_payload("agent-m")
    # Assert
    assert set(payload.keys()) == {"one", "two"}
