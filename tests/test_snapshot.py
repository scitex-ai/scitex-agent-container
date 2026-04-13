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
        "mem_total_bytes": 1024,
        "mem_used_bytes": 512,
        "mem_free_bytes": 512,
        "nproc_cur": 100,
        "nproc_max": 1000,
        "fork_pressure_pct": 10.0,
        "context_percent": None,
        "pids": {
            "container_daemon": 1,
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


def test_probe_mem_darwin_counts_inactive_and_speculative(monkeypatch):
    """Darwin mem must count free + inactive + speculative as available.

    Regression for msg#8603 / todo#310: the previous implementation only
    summed free + speculative, which fired false-positive mem-CRITICAL
    alerts on MBA where ~6GB lived in inactive+speculative cache while
    free was a typical ~100MB.
    """
    monkeypatch.setattr(snap_mod.platform, "system", lambda: "Darwin")

    page_size = 4096
    # 16 GB total, only 25600 pages (100 MB) "free", 1.5M pages (6 GB)
    # inactive, 50k pages (200 MB) speculative — typical macOS at idle.
    free_pgs = 25_600
    inactive_pgs = 1_572_864
    speculative_pgs = 51_200
    total_bytes = 16 * 1024 ** 3
    expected_free_bytes = (free_pgs + inactive_pgs + speculative_pgs) * page_size

    vm_stat_output = (
        "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        f"Pages free:                              {free_pgs}.\n"
        f"Pages active:                            500000.\n"
        f"Pages inactive:                          {inactive_pgs}.\n"
        f"Pages speculative:                       {speculative_pgs}.\n"
        f"Pages wired down:                        200000.\n"
    )

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["/usr/sbin/sysctl", "-n"] and cmd[2] == "hw.memsize":
            return _FakeCompleted(stdout=f"{total_bytes}\n", returncode=0)
        if cmd[0] == "vm_stat":
            return _FakeCompleted(stdout=vm_stat_output, returncode=0)
        return _FakeCompleted(stdout="", returncode=0)

    monkeypatch.setattr(snap_mod.subprocess, "run", fake_run)

    total, used, free = snap_mod._probe_mem_darwin()
    assert total == total_bytes
    assert free == expected_free_bytes
    # Used must reflect the realistic (~9.7 GB) figure, not the broken
    # ~15.9 GB that the old "free + speculative only" math produced.
    assert used == total_bytes - expected_free_bytes
    # Sanity: free should be measured in GBs, not MBs.
    assert free > 1024 ** 3, f"free={free} suggests inactive pages were dropped"


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


def test_take_snapshot_concurrent_write_is_serialized(tmp_path, monkeypatch):
    """10 threads racing on the same agent must leave a consistent state."""
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a, tmux_count=1)
    )

    # First seed so prev will be written too.
    snap_mod.take_snapshot("concur")

    # Now race: each thread bumps tmux_count to a unique value so diffs fire.
    N = 10
    barrier = threading.Barrier(N)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            monkey_snap = _fake_snap("concur", tmux_count=100 + i)
            # Local override of gather — but we must be careful: monkeypatch
            # is not thread-safe. Instead, patch once with an id-varying fn.
            barrier.wait()
            snap_mod.take_snapshot("concur")
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    # Use a single thread-safe gather that cycles values via a counter.
    counter = {"n": 100}
    counter_lock = threading.Lock()

    def racy_gather(a, session=None):
        with counter_lock:
            counter["n"] += 1
            val = counter["n"]
        return _fake_snap(a, tmux_count=val)

    monkeypatch.setattr(snap_mod, "gather_snapshot", racy_gather)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"worker exceptions: {errors}"

    latest = tmp_path / "concur.latest.json"
    prev = tmp_path / "concur.prev.json"
    lock = tmp_path / "concur.lock"

    assert latest.exists()
    assert prev.exists()
    # Both files must parse as valid JSON (no torn writes).
    json.loads(latest.read_text())
    json.loads(prev.read_text())

    # No .tmp stragglers left behind.
    stragglers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert stragglers == [], f"unexpected tmp files: {stragglers}"

    # Expected file set only.
    names = sorted(p.name for p in tmp_path.iterdir())
    # diff.json may or may not be present depending on timing, but all names
    # must belong to the expected set.
    allowed = {
        "concur.latest.json",
        "concur.prev.json",
        "concur.lock",
        "concur.diff.json",
    }
    assert set(names).issubset(allowed), f"unexpected files: {set(names) - allowed}"
    assert lock.exists()


def test_take_snapshot_lock_file_is_reusable(tmp_path, monkeypatch):
    """Advisory lock is released on fd close; subsequent calls succeed."""
    monkeypatch.setattr(
        snap_mod, "gather_snapshot", lambda a, session=None: _fake_snap(a)
    )
    snap_mod.take_snapshot("reuse")
    lock = tmp_path / "reuse.lock"
    assert lock.exists()
    # A second call must not hang and must succeed (lock was released).
    snap_mod.take_snapshot("reuse")
    assert (tmp_path / "reuse.latest.json").exists()
    assert (tmp_path / "reuse.prev.json").exists()


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


# ---------------------------------------------------------------------------
# --terse projection (todo#300)
# ---------------------------------------------------------------------------


def test_snapshot_terse_emits_only_whitelisted_fields() -> None:
    from scitex_agent_container.terse import TERSE_SNAPSHOT_FIELDS, project_terse

    full = _fake_snap("t1")
    full["has_diff"] = False
    full["diff_fields"] = []
    full["tmux_names"] = ["a", "b"]  # bulky array — must not leak
    full["agent_meta"] = {"context_pct": 12.3, "current_tool": "Bash"}

    terse = project_terse(full, TERSE_SNAPSHOT_FIELDS)
    assert set(terse.keys()) == set(TERSE_SNAPSHOT_FIELDS)
    assert terse["agent"] == "t1"
    assert terse["tmux_count"] == 2
    assert terse["pids.claude_code"] == 2
    assert terse["pids.container_daemon"] == 1
    assert terse["has_diff"] is False
    # Bulky fields must not appear
    assert "tmux_names" not in terse
    assert "agent_meta" not in terse
    assert "diff_fields" not in terse


def test_snapshot_terse_absent_fields_emit_null() -> None:
    from scitex_agent_container.terse import TERSE_SNAPSHOT_FIELDS, project_terse

    full = {"agent": "bare"}
    terse = project_terse(full, TERSE_SNAPSHOT_FIELDS)
    assert terse["host"] is None
    assert terse["tmux_count"] is None
    assert terse["pids.claude_code"] is None
    assert terse["pids.container_daemon"] is None
    assert set(terse.keys()) == set(TERSE_SNAPSHOT_FIELDS)


def test_snapshot_terse_byte_size_is_smaller() -> None:
    """Terse payload must be dramatically smaller than full on a realistic fixture."""
    from scitex_agent_container.terse import TERSE_SNAPSHOT_FIELDS, project_terse

    full = _fake_snap("real")
    # Bulk the snapshot up to match reality post-#286.
    full["has_diff"] = True
    full["diff_fields"] = ["tmux_count", "claude_procs", "mem_used_bytes"]
    full["tmux_names"] = [f"session-{i}" for i in range(40)]
    full["agent_meta"] = {
        "context_pct": 57.5,
        "current_tool": "Bash",
        "current_task": "x" * 500,
        "last_activity": "2026-04-13T00:00:00Z",
        "subagents": [{"id": i, "name": f"sub-{i}"} for i in range(10)],
        "skills_loaded": [f"skill-{i}" for i in range(20)],
        "model": "claude-opus-4-6",
        "big_blob": "z" * 4000,
    }
    full["pids"]["sidecars"] = {
        f"side-{i}": {"kind": "thread", "pid": i, "alive": True}
        for i in range(10)
    }

    full_bytes = len(json.dumps(full))
    terse_bytes = len(json.dumps(project_terse(full, TERSE_SNAPSHOT_FIELDS)))
    ratio = full_bytes / max(terse_bytes, 1)
    assert ratio >= 5, f"terse only {ratio:.1f}x smaller ({full_bytes}B -> {terse_bytes}B)"


def test_status_terse_byte_size_is_smaller() -> None:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    # Simulate a real status --json payload post-#286.
    full = {
        "name": "real",
        "agent": "real",
        "state": "running",
        "timestamp": "2026-04-13T00:00:00Z",
        "tmux_alive": True,
        "last_post_ts": "2026-04-13T00:00:00Z",
        "config": "/very/long/path/to/config.yaml",
        "screen": "real",
        "started_at": "2026-04-13T00:00:00Z",
        "status": "running",
        "model": "claude-opus-4-6",
        "runtime": "local",
        "hooks_configured": {f"hook{i}": i for i in range(7)},
        "listen": [{"port": 8000 + i, "proto": "http"} for i in range(5)],
        "extensions": {"big": "y" * 3000},
        "context_management": {
            "percent": 42.0,
            "strategy": "compact",
            "trigger_at_percent": 85,
        },
        "agent_meta": {"big": "z" * 5000, "skills_loaded": list(range(30))},
        "skills_loaded": [f"skill-{i}" for i in range(20)],
        "pids": {"claude_code": 1, "container_daemon": 2, "sidecars": {"s": {}}},
        "health": {"ok": True, "details": "x" * 500},
        "snapshot": {
            "timestamp": "2026-04-13T00:00:00Z",
            "has_diff": True,
            "diff_fields": ["tmux_count", "claude_procs"],
        },
    }
    full_bytes = len(json.dumps(full))
    terse_bytes = len(json.dumps(project_terse(full, TERSE_STATUS_FIELDS)))
    ratio = full_bytes / max(terse_bytes, 1)
    assert ratio >= 5, f"terse only {ratio:.1f}x smaller ({full_bytes}B -> {terse_bytes}B)"
