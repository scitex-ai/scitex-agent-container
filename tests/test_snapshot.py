"""Tests for scitex_agent_container.snapshot (todo#286)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scitex_agent_container import snapshot as snap_mod


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the snapshot cache dir to tmp_path for every test."""
    monkeypatch.setenv("SCITEX_AGENT_CACHE_DIR", str(tmp_path))
    # Reset the sidecar registry between tests so state doesn't leak.
    snap_mod._SIDECARS.clear()
    yield
    snap_mod._SIDECARS.clear()


def _fake_snap(agent: str, **overrides):
    base = {
        "agent": agent,
        "timestamp": "2026-04-13T00:00:00+00:00",
        "host": "test-host",
        "tmux_count": 2,
        "tmux_names": ["a", "b"],
        "screen_count": 0,
        "claude_procs": 1,
        "bun_procs": 0,
        "node_procs": 0,
        "load1": 0.5,
        "mem_total": 1024,
        "mem_used": 512,
        "mem_free": 512,
        "nproc_cur": 100,
        "nproc_max": 1000,
        "fork_pressure_pct": 10.0,
        "context_percent": None,
        "pids": {
            "agent": 1,
            "claude_code": 2,
            "tmux": {"server": 3, "pane": 4},
            "sidecars": {},
        },
    }
    base.update(overrides)
    return base


def test_snapshot_writes_latest_and_prev(tmp_path, monkeypatch):
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap1 = snap_mod.take_snapshot("a1")
    latest = tmp_path / "a1.latest.json"
    prev = tmp_path / "a1.prev.json"
    assert latest.exists()
    assert not prev.exists()  # no prev on first call
    assert snap1["has_diff"] is False
    assert snap1["diff_fields"] == []

    # Second call with a change should roll prev and produce a diff.
    monkeypatch.setattr(
        snap_mod,
        "gather_snapshot",
        lambda a, session=None: _fake_snap(a, tmux_count=3),
    )
    snap2 = snap_mod.take_snapshot("a1")
    assert prev.exists()
    assert latest.exists()
    assert "tmux_count" in snap2["diff_fields"]


def test_snapshot_diff_fields_detects_tmux_count_change(monkeypatch):
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap_mod.take_snapshot("a2")
    monkeypatch.setattr(
        snap_mod,
        "gather_snapshot",
        lambda a, session=None: _fake_snap(a, tmux_count=9),
    )
    snap2 = snap_mod.take_snapshot("a2")
    assert snap2["has_diff"] is True
    assert "tmux_count" in snap2["diff_fields"]


def test_snapshot_diff_empty_on_first_run(monkeypatch):
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap1 = snap_mod.take_snapshot("first")
    assert snap1["has_diff"] is False
    assert snap1["diff_fields"] == []


def test_snapshot_pids_sidecar_alive_for_process(monkeypatch):
    snap_mod.register_sidecar("p1", kind="process", name="health_monitor", pid=4242)
    killed: list[int] = []

    def fake_kill(pid, sig):
        killed.append(pid)
        return None  # alive

    monkeypatch.setattr(snap_mod.os, "kill", fake_kill)
    payload = snap_mod._sidecars_payload("p1")
    assert payload["health_monitor"]["kind"] == "process"
    assert payload["health_monitor"]["pid"] == 4242
    assert payload["health_monitor"]["alive"] is True
    assert killed == [4242]

    def dead_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(snap_mod.os, "kill", dead_kill)
    payload = snap_mod._sidecars_payload("p1")
    assert payload["health_monitor"]["alive"] is False


def test_snapshot_pids_sidecar_alive_for_thread():
    ev = threading.Event()

    def runner():
        ev.wait(timeout=5)

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    snap_mod.register_sidecar("t1", kind="thread", name="context_manager", thread=th)
    payload = snap_mod._sidecars_payload("t1")
    assert payload["context_manager"]["kind"] == "thread"
    assert payload["context_manager"]["alive"] is True
    ev.set()
    th.join(timeout=5)
    payload = snap_mod._sidecars_payload("t1")
    assert payload["context_manager"]["alive"] is False


def test_status_exposes_snapshot_from_cache(tmp_path, monkeypatch):
    from scitex_agent_container import lifecycle
    from scitex_agent_container.config import AgentConfig

    ws = tmp_path / "ws"
    ws.mkdir()

    fake_latest = _fake_snap("x1")
    fake_latest["has_diff"] = True
    fake_latest["diff_fields"] = ["tmux_count"]
    (tmp_path / "x1.latest.json").write_text(json.dumps(fake_latest))

    cfg = AgentConfig(name="x1", screen_name="x1", workdir=str(ws))

    class _FakeRegistry:
        def get(self, name):
            return {
                "config": str(ws / "fake.yaml"),
                "screen": "x1",
                "started_at": "2026-04-13T00:00:00Z",
            }

    class _FakeRT:
        def is_running(self, _c):
            return True

    monkeypatch.setattr(lifecycle, "load_config", lambda _p: cfg)
    monkeypatch.setattr(lifecycle, "_get_runtime", lambda _c: _FakeRT())

    result = lifecycle.agent_status("x1", registry=_FakeRegistry())
    assert result["snapshot"] is not None
    assert result["snapshot"]["has_diff"] is True
    assert result["snapshot"]["diff_fields"] == ["tmux_count"]


def test_snapshot_env_override_cache_dir(tmp_path, monkeypatch):
    alt = tmp_path / "alt-cache"
    monkeypatch.setenv("SCITEX_AGENT_CACHE_DIR", str(alt))
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap_mod.take_snapshot("envy")
    assert (alt / "envy.latest.json").exists()


def test_tmux_names_is_array(monkeypatch):
    # Regression guard: tmux_names must remain a JSON array, not joined.
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap = snap_mod.take_snapshot("arrayguard")
    assert isinstance(snap["tmux_names"], list)
    # Ensure serialization round-trips as a list.
    latest = Path(snap_mod._latest_path("arrayguard"))
    data = json.loads(latest.read_text())
    assert isinstance(data["tmux_names"], list)
