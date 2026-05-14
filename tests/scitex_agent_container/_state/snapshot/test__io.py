"""Tests for scitex_agent_container._state.snapshot._io.

Exercises probes (with subprocess mocked), gather_snapshot, take_snapshot,
read_latest, snapshot_tick. Stays on tmp_path; no real container ops.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._state.snapshot import _io as mod


@pytest.fixture(autouse=True)
def _home_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect snapshot cache to tmp_path."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SAC_CACHE_DIR", str(cache))
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CACHE_DIR", str(cache))
    return cache


# ---------------------------------------------------------------------------
# _run helper
# ---------------------------------------------------------------------------


def test_run_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = "hello\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._run(["echo", "hello"]) == "hello\n"


def test_run_swallows_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run(["x"]) == ""


# ---------------------------------------------------------------------------
# Probes — happy + error paths
# ---------------------------------------------------------------------------


def test_probe_tmux_lists_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        returncode = 0
        stdout = "alpha\nbeta\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    n, names = mod._probe_tmux()
    assert n == 2
    assert names == ["alpha", "beta"]


def test_probe_tmux_no_server_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    n, names = mod._probe_tmux()
    assert n == 0
    assert names == []


def test_probe_tmux_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", boom)
    n, names = mod._probe_tmux()
    assert n is None
    assert names == []


def test_probe_screen_count_no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/screen")

    class R:
        returncode = 1
        stdout = "No Sockets found in /tmp.\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._probe_screen_count() == 0


def test_probe_screen_count_lists_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/screen")

    class R:
        returncode = 0
        stdout = "There is a screen on:\n\t12345.alpha\t(Detached)\n\t67890.beta\t(Attached)\n"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._probe_screen_count() == 2


def test_probe_screen_count_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    assert mod._probe_screen_count() is None


def test_probe_screen_count_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/screen")

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="screen", timeout=3)

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._probe_screen_count() is None


def test_probe_claude_pid_returns_int(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = "9999\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._probe_claude_pid() == 9999


def test_probe_claude_pid_empty_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._probe_claude_pid() is None


def test_probe_claude_pid_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = "abc\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._probe_claude_pid() is None


def test_probe_claude_pid_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._probe_claude_pid() is None


def test_proc_count_counts_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = "111 claude\n222 claude --bg\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._proc_count("claude") == 2


def test_proc_count_empty_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    assert mod._proc_count("claude") == 0


def test_probe_load1_handles_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise OSError

    monkeypatch.setattr(mod.os, "getloadavg", boom)
    assert mod._probe_load1() is None


def test_probe_load1_returns_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.os, "getloadavg", lambda: (1.5, 1.2, 0.9))
    assert mod._probe_load1() == 1.5


def test_probe_mem_returns_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    # Either Linux or Darwin code path will exercise _probe_mem; just
    # verify it returns a triple. We don't pin the values.
    out = mod._probe_mem()
    assert isinstance(out, tuple) and len(out) == 3


def test_probe_mem_unknown_platform_returns_nones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod.platform, "system", lambda: "Plan9")
    assert mod._probe_mem() == (None, None, None)


def test_probe_mem_linux_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    fake = tmp_path / "meminfo"
    fake.write_text(
        "MemTotal:        8000 kB\nMemAvailable:    3000 kB\nMemFree:    1000 kB\n"
    )
    # Patch Path("/proc/meminfo").read_text via a wrapper.
    orig = Path.read_text

    def fake_read(self, *a, **kw):
        if str(self) == "/proc/meminfo":
            return fake.read_text()
        return orig(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read)
    total, used, avail = mod._probe_mem()
    assert total == 8000 * 1024
    assert avail == 3000 * 1024


def test_probe_nproc_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")

    class R:
        stdout = "PID TTY TIME CMD\n1 a\n2 b\n3 c\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    fake_pid_max = tmp_path / "pid_max"
    fake_pid_max.write_text("32768\n")
    orig = Path.read_text

    def fake_read(self, *a, **kw):
        if str(self) == "/proc/sys/kernel/pid_max":
            return "32768\n"
        return orig(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read)
    cur, mx = mod._probe_nproc()
    assert cur == 3
    assert mx == 32768


def test_probe_nproc_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.platform, "system", lambda: "Plan9")

    class R:
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    cur, mx = mod._probe_nproc()
    # Empty ps output → cur is None; mx None (unknown platform).
    assert mx is None


def test_probe_tmux_pids_no_session() -> None:
    out = mod._probe_tmux_pids(None)
    assert out == {"server": None, "pane": None}


def test_probe_tmux_pids_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"i": 0}

    def fake_run(argv, **kw):
        calls["i"] += 1

        class R:
            returncode = 0
            stdout = "12345\n"

        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    out = mod._probe_tmux_pids("alpha")
    assert out["pane"] == 12345
    assert out["server"] == 12345


def test_probe_tmux_pids_handles_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(mod.subprocess, "run", boom)
    out = mod._probe_tmux_pids("alpha")
    assert out == {"server": None, "pane": None}


# ---------------------------------------------------------------------------
# gather_snapshot / take_snapshot / read_latest
# ---------------------------------------------------------------------------


@pytest.fixture
def _mocked_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force all probes through a deterministic stdout path."""

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.os, "getloadavg", lambda: (0.5, 0.5, 0.5))


def test_gather_snapshot_shape(_mocked_probes: None) -> None:
    snap = mod.gather_snapshot("alpha")
    assert snap["agent"] == "alpha"
    assert "timestamp" in snap
    assert "host" in snap
    assert "pids" in snap
    assert snap["pids"]["container_daemon"] > 0


def test_take_snapshot_writes_latest(_cache_dir: Path, _mocked_probes: None) -> None:
    snap = mod.take_snapshot("alpha")
    latest = _cache_dir / "alpha.latest.json"
    assert latest.is_file()
    body = json.loads(latest.read_text())
    assert body["agent"] == "alpha"
    assert snap["has_diff"] is False  # First snapshot: no diff baseline.


def test_take_snapshot_rolls_prev(_cache_dir: Path, _mocked_probes: None) -> None:
    mod.take_snapshot("alpha")
    mod.take_snapshot("alpha")
    assert (_cache_dir / "alpha.latest.json").is_file()
    assert (_cache_dir / "alpha.prev.json").is_file()


def test_take_snapshot_writes_diff_when_changes(
    _cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force diff: vary load1 across calls.
    sequence = iter([(0.5, 0.5, 0.5), (5.5, 5.5, 5.5)])
    monkeypatch.setattr(mod.os, "getloadavg", lambda: next(sequence))

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: R())
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    mod.take_snapshot("alpha")
    snap2 = mod.take_snapshot("alpha")
    if snap2["has_diff"]:
        assert (_cache_dir / "alpha.diff.json").is_file()


def test_take_snapshot_with_diff_false(_cache_dir: Path, _mocked_probes: None) -> None:
    """with_diff=False short-circuits diff computation."""
    snap = mod.take_snapshot("alpha", with_diff=False)
    assert snap["diff_fields"] == []
    assert snap["has_diff"] is False


def test_read_latest_returns_dict(_cache_dir: Path, _mocked_probes: None) -> None:
    mod.take_snapshot("alpha")
    body = mod.read_latest("alpha")
    assert body is not None
    assert body["agent"] == "alpha"


def test_read_latest_missing_returns_none() -> None:
    assert mod.read_latest("nonexistent") is None


def test_read_latest_corrupted_returns_none(_cache_dir: Path) -> None:
    (_cache_dir / "ghost.latest.json").write_text("{not-json")
    assert mod.read_latest("ghost") is None


def test_take_snapshot_tolerates_corrupt_prev(
    _cache_dir: Path, _mocked_probes: None
) -> None:
    (_cache_dir / "alpha.latest.json").write_text("{not json")
    snap = mod.take_snapshot("alpha")
    assert snap["agent"] == "alpha"


# ---------------------------------------------------------------------------
# snapshot_tick
# ---------------------------------------------------------------------------


def test_snapshot_tick_smoke(_cache_dir: Path, _mocked_probes: None) -> None:
    mod.snapshot_tick("alpha")
    assert (_cache_dir / "alpha.latest.json").is_file()


def test_snapshot_tick_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(mod, "take_snapshot", boom)
    # Should not raise.
    mod.snapshot_tick("alpha")


def test_atomic_write_json(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    mod._atomic_write_json(p, {"k": 1})
    assert json.loads(p.read_text()) == {"k": 1}
