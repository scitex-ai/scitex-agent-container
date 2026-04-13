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
    # Stub out the agent_meta shell-out; tests that need it override.
    monkeypatch.setattr(snap_mod, "fetch_agent_meta", lambda *a, **k: None)
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


def test_snapshot_includes_agent_meta_when_available(monkeypatch):
    """A live ContextManager with last_meta exposes it in gather_snapshot."""
    from scitex_agent_container import context_manager as cm_mod
    from scitex_agent_container.config import ContextManagementConfig

    sample_meta = {
        "agent": "live1",
        "alive": True,
        "subagents": 2,
        "context_pct": 57.5,
        "current_tool": "Bash",
        "last_activity": "2026-04-13T05:12:00.665Z",
        "model": "claude-opus-4-6",
        "extra_field_ignored": "yes",
    }
    fake = cm_mod.ContextManager(
        agent_name="live1",
        session_name="live1",
        config=ContextManagementConfig(),
        dispatcher=lambda *a, **k: None,
        capture=lambda _s: "",
    )
    fake.last_meta = sample_meta
    fake.last_percent = 57.5
    monkeypatch.setitem(cm_mod._SENSORS, "live1", fake)
    try:
        snap = snap_mod.gather_snapshot("live1", session="live1")
    finally:
        cm_mod._SENSORS.pop("live1", None)

    assert snap["agent_meta"] is not None
    assert snap["agent_meta"]["context_pct"] == 57.5
    assert snap["agent_meta"]["current_tool"] == "Bash"
    assert snap["agent_meta"]["model"] == "claude-opus-4-6"
    # Projected down to known keys (no extras)
    assert "extra_field_ignored" not in snap["agent_meta"]
    assert snap["context_percent"] == 57.5


def test_snapshot_agent_meta_null_when_unavailable():
    """No sensor + fetch_agent_meta returning None → agent_meta is None."""
    snap = snap_mod.gather_snapshot("ghost", session="ghost")
    assert snap["agent_meta"] is None


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_screen_count_zero_when_installed_no_sessions(monkeypatch):
    """screen installed + no live sockets → 0 (not None)."""
    monkeypatch.setattr(
        snap_mod.shutil, "which", lambda name: "/usr/bin/screen" if name == "screen" else None
    )

    def fake_run(cmd, *a, **kw):
        assert cmd[0] == "screen"
        return _FakeCompleted(
            stdout="No Sockets found in /var/run/screen/S-user.\n",
            stderr="",
            returncode=1,
        )

    monkeypatch.setattr(snap_mod.subprocess, "run", fake_run)
    result = snap_mod._probe_screen_count()
    assert result == 0
    assert result is not None


def test_screen_count_none_when_not_installed(monkeypatch):
    monkeypatch.setattr(snap_mod.shutil, "which", lambda name: None)
    assert snap_mod._probe_screen_count() is None


def test_screen_count_positive_when_sessions_listed(monkeypatch):
    monkeypatch.setattr(
        snap_mod.shutil, "which", lambda name: "/usr/bin/screen"
    )
    listing = (
        "There are screens on:\n"
        "\t12345.head-mba\t(Detached)\n"
        "\t12346.worker-1\t(Attached)\n"
        "2 Sockets in /var/run/screen/S-user.\n"
    )
    monkeypatch.setattr(
        snap_mod.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout=listing, returncode=0),
    )
    assert snap_mod._probe_screen_count() == 2


def test_claude_pid_does_not_match_container_python(monkeypatch):
    """pgrep -x claude must not match the container python wrapper."""
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        # pgrep -x claude only returns PIDs whose comm == "claude",
        # so the python container wrapper (comm == "python3") is excluded.
        assert cmd == ["pgrep", "-n", "-x", "claude"]
        return _FakeCompleted(stdout="12345\n", returncode=0)

    monkeypatch.setattr(snap_mod.subprocess, "run", fake_run)
    assert snap_mod._probe_claude_pid() == 12345
    # Sanity: the query we issued is command-name exact (-x), not full -f.
    assert "-x" in captured["cmd"]
    assert "-f" not in captured["cmd"]


def test_claude_pid_none_when_no_match(monkeypatch):
    monkeypatch.setattr(
        snap_mod.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout="", returncode=1),
    )
    assert snap_mod._probe_claude_pid() is None


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
