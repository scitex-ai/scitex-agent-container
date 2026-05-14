"""Tests for scitex_agent_container._state.snapshot._io (no-mocks).

Probes are exercised by installing PATH-prepended fake binaries via the
``subprocess_shim`` fixture so production code calls real
``subprocess.run`` against a controlled shim. Cache dir is redirected to
``tmp_path`` via the real ``SAC_CACHE_DIR`` /
``SCITEX_AGENT_CONTAINER_CACHE_DIR`` env vars (read by production).
Snapshot round-trips read the JSON we wrote on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._state.snapshot import _io as mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path, env_save_restore) -> Path:
    """Redirect snapshot cache to a tmp_path via real env-var seam."""
    cache = tmp_path / "cache"
    cache.mkdir()
    # Production reads via _env.getenv(...) which honors both forms; set
    # both to the same value to satisfy the conflict guard.
    env_save_restore.set("SAC_CACHE_DIR", str(cache))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def isolated_path(tmp_path: Path, env_save_restore):
    """Replace PATH with an empty tmp dir so unshimmed probes fail naturally.

    Tests that need a specific binary install it via ``subprocess_shim``
    (which prepends its own bin dir). With no shim and an isolated PATH,
    ``subprocess.run(["tmux", ...])`` raises ``FileNotFoundError`` —
    which is exactly the missing-binary path production guards.
    """
    empty = tmp_path / "_empty_bin"
    empty.mkdir()
    env_save_restore.set("PATH", str(empty))
    return empty


# ---------------------------------------------------------------------------
# _run helper
# ---------------------------------------------------------------------------


def test_run_returns_stdout(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("myecho", stdout="hello\n")
    # Act
    result = mod._run(["myecho"])
    # Assert
    assert result == "hello\n"


def test_run_swallows_missing_binary(isolated_path) -> None:
    # Arrange: isolated_path fixture already empties PATH.
    # Act
    result = mod._run(["definitely-not-a-real-binary-xyz"])
    # Assert
    assert result == ""


# ---------------------------------------------------------------------------
# _probe_tmux
# ---------------------------------------------------------------------------


def test_probe_tmux_lists_sessions_count(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("tmux", stdout="alpha\nbeta\n", exit=0)
    # Act
    n, _names = mod._probe_tmux()
    # Assert
    assert n == 2


def test_probe_tmux_lists_sessions_names(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("tmux", stdout="alpha\nbeta\n", exit=0)
    # Act
    _n, names = mod._probe_tmux()
    # Assert
    assert names == ["alpha", "beta"]


def test_probe_tmux_no_server_returns_zero_count(subprocess_shim) -> None:
    # Arrange: tmux returncode != 0 with empty stdout -> "no server running".
    subprocess_shim.install("tmux", stdout="", exit=1)
    # Act
    n, _names = mod._probe_tmux()
    # Assert
    assert n == 0


def test_probe_tmux_no_server_returns_empty_names(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("tmux", stdout="", exit=1)
    # Act
    _n, names = mod._probe_tmux()
    # Assert
    assert names == []


def test_probe_tmux_missing_binary_count(isolated_path) -> None:
    # Arrange: isolated_path empties PATH of tmux.
    # Act
    n, _names = mod._probe_tmux()
    # Assert
    assert n is None


def test_probe_tmux_missing_binary_names(isolated_path) -> None:
    # Arrange
    # Act
    _n, names = mod._probe_tmux()
    # Assert
    assert names == []


# ---------------------------------------------------------------------------
# _probe_screen_count
# ---------------------------------------------------------------------------


def test_probe_screen_count_no_sockets(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("screen", stdout="No Sockets found in /tmp.\n", exit=1)
    # Act
    result = mod._probe_screen_count()
    # Assert
    assert result == 0


def test_probe_screen_count_lists_sessions(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install(
        "screen",
        stdout=(
            "There is a screen on:\n\t12345.alpha\t(Detached)\n"
            "\t67890.beta\t(Attached)\n"
        ),
        exit=0,
    )
    # Act
    result = mod._probe_screen_count()
    # Assert
    assert result == 2


def test_probe_screen_count_missing_binary(isolated_path) -> None:
    # Arrange: shutil.which("screen") returns None against isolated PATH.
    # Act
    result = mod._probe_screen_count()
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# _probe_claude_pid / _proc_count
# ---------------------------------------------------------------------------


def test_probe_claude_pid_returns_int(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("pgrep", stdout="9999\n", exit=0)
    # Act
    pid = mod._probe_claude_pid()
    # Assert
    assert pid == 9_999


def test_probe_claude_pid_empty_stdout(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("pgrep", stdout="", exit=1)
    # Act
    pid = mod._probe_claude_pid()
    # Assert
    assert pid is None


def test_probe_claude_pid_non_numeric(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("pgrep", stdout="abc\n", exit=0)
    # Act
    pid = mod._probe_claude_pid()
    # Assert
    assert pid is None


def test_probe_claude_pid_missing_binary(isolated_path) -> None:
    # Arrange: no pgrep on PATH.
    # Act
    pid = mod._probe_claude_pid()
    # Assert
    assert pid is None


def test_proc_count_counts_lines(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("pgrep", stdout="111 claude\n222 claude --bg\n", exit=0)
    # Act
    count = mod._proc_count("claude")
    # Assert
    assert count == 2


def test_proc_count_empty_returns_zero(subprocess_shim) -> None:
    # Arrange
    subprocess_shim.install("pgrep", stdout="", exit=1)
    # Act
    count = mod._proc_count("claude")
    # Assert
    assert count == 0


# ---------------------------------------------------------------------------
# _probe_load1, _probe_mem, _probe_nproc — real platform calls
# ---------------------------------------------------------------------------


def test_probe_load1_real_call_type() -> None:
    # Arrange: real OS call.
    # Act
    out = mod._probe_load1()
    # Assert: float on Linux/Darwin, None on platforms without getloadavg.
    assert out is None or (isinstance(out, float) and out >= 0.0)


def test_probe_mem_returns_triple_shape() -> None:
    # Arrange: real OS call.
    # Act
    out = mod._probe_mem()
    # Assert
    assert isinstance(out, tuple) and len(out) == 3


def test_probe_mem_total_is_positive_on_real_host() -> None:
    # Arrange: real OS call.
    # Act
    total, _used, _avail = mod._probe_mem()
    # Assert
    assert total is None or total > 0


def test_probe_nproc_current_is_nonnegative() -> None:
    # Arrange: real OS call.
    # Act
    cur, _mx = mod._probe_nproc()
    # Assert
    assert cur is None or cur >= 0


def test_probe_nproc_max_is_positive() -> None:
    # Arrange: real OS call.
    # Act
    _cur, mx = mod._probe_nproc()
    # Assert
    assert mx is None or mx > 0


# ---------------------------------------------------------------------------
# _probe_tmux_pids
# ---------------------------------------------------------------------------


def test_probe_tmux_pids_no_session() -> None:
    # Arrange
    # Act
    out = mod._probe_tmux_pids(None)
    # Assert
    assert out == {"server": None, "pane": None}


def test_probe_tmux_pids_resolved_pane(subprocess_shim) -> None:
    # Arrange: tmux display ... prints the pane pid.
    subprocess_shim.install("tmux", stdout="12345\n", exit=0)
    subprocess_shim.install("pgrep", stdout="12345\n", exit=0)
    # Act
    out = mod._probe_tmux_pids("alpha")
    # Assert
    assert out["pane"] == 12_345


def test_probe_tmux_pids_resolved_server(subprocess_shim) -> None:
    # Arrange: pgrep -n -x tmux prints the server pid.
    subprocess_shim.install("tmux", stdout="12345\n", exit=0)
    subprocess_shim.install("pgrep", stdout="12345\n", exit=0)
    # Act
    out = mod._probe_tmux_pids("alpha")
    # Assert
    assert out["server"] == 12_345


def test_probe_tmux_pids_missing_binaries(isolated_path) -> None:
    # Arrange: neither tmux nor pgrep on PATH.
    # Act
    out = mod._probe_tmux_pids("alpha")
    # Assert
    assert out == {"server": None, "pane": None}


# ---------------------------------------------------------------------------
# gather_snapshot / take_snapshot / read_latest
# ---------------------------------------------------------------------------


def test_gather_snapshot_agent_field(cache_dir: Path, isolated_path) -> None:
    # Arrange
    # Act
    snap = mod.gather_snapshot("alpha")
    # Assert
    assert snap["agent"] == "alpha"


def test_gather_snapshot_contains_timestamp(cache_dir: Path, isolated_path) -> None:
    # Arrange
    # Act
    snap = mod.gather_snapshot("alpha")
    # Assert
    assert "timestamp" in snap


def test_gather_snapshot_records_container_daemon_pid(
    cache_dir: Path, isolated_path
) -> None:
    # Arrange
    # Act
    snap = mod.gather_snapshot("alpha")
    # Assert
    assert snap["pids"]["container_daemon"] == os.getpid()


def test_take_snapshot_writes_latest_file(cache_dir: Path, isolated_path) -> None:
    # Arrange
    # Act
    mod.take_snapshot("alpha")
    # Assert
    assert (cache_dir / "alpha.latest.json").is_file()


def test_take_snapshot_first_call_has_no_diff(cache_dir: Path, isolated_path) -> None:
    # Arrange: no prior snapshot exists.
    # Act
    snap = mod.take_snapshot("alpha")
    # Assert
    assert snap["has_diff"] is False


def test_take_snapshot_latest_payload_round_trips(
    cache_dir: Path, isolated_path
) -> None:
    # Arrange
    mod.take_snapshot("alpha")
    # Act
    body = json.loads((cache_dir / "alpha.latest.json").read_text())
    # Assert
    assert body["agent"] == "alpha"


def test_take_snapshot_rolls_prev_after_two_calls(
    cache_dir: Path, isolated_path
) -> None:
    # Arrange
    mod.take_snapshot("alpha")
    # Act
    mod.take_snapshot("alpha")
    # Assert
    assert (cache_dir / "alpha.prev.json").is_file()


def test_take_snapshot_writes_diff_when_tmux_changes(
    cache_dir: Path, subprocess_shim
) -> None:
    # Arrange: first snapshot has one tmux session.
    subprocess_shim.install("tmux", stdout="alpha\n", exit=0)
    mod.take_snapshot("alpha")
    # Re-install the shim so its stdout changes -> different tmux_names.
    subprocess_shim.install("tmux", stdout="alpha\nbeta\n", exit=0)
    # Act
    snap2 = mod.take_snapshot("alpha")
    # Assert
    assert snap2["has_diff"] is True


def test_take_snapshot_writes_diff_file_when_changes(
    cache_dir: Path, subprocess_shim
) -> None:
    # Arrange
    subprocess_shim.install("tmux", stdout="alpha\n", exit=0)
    mod.take_snapshot("alpha")
    subprocess_shim.install("tmux", stdout="alpha\nbeta\n", exit=0)
    # Act
    mod.take_snapshot("alpha")
    # Assert
    assert (cache_dir / "alpha.diff.json").is_file()


def test_take_snapshot_with_diff_false_yields_empty_diff_fields(
    cache_dir: Path, isolated_path
) -> None:
    # Arrange
    # Act
    snap = mod.take_snapshot("alpha", with_diff=False)
    # Assert
    assert snap["diff_fields"] == []


def test_take_snapshot_with_diff_false_yields_has_diff_false(
    cache_dir: Path, isolated_path
) -> None:
    # Arrange
    # Act
    snap = mod.take_snapshot("alpha", with_diff=False)
    # Assert
    assert snap["has_diff"] is False


def test_read_latest_returns_persisted_payload(cache_dir: Path, isolated_path) -> None:
    # Arrange
    mod.take_snapshot("alpha")
    # Act
    body = mod.read_latest("alpha")
    # Assert
    assert body is not None and body["agent"] == "alpha"


def test_read_latest_missing_returns_none(cache_dir: Path) -> None:
    # Arrange: no snapshot ever taken for this agent.
    # Act
    body = mod.read_latest("nonexistent")
    # Assert
    assert body is None


def test_read_latest_corrupted_returns_none(cache_dir: Path) -> None:
    # Arrange
    (cache_dir / "ghost.latest.json").write_text("{not-json")
    # Act
    body = mod.read_latest("ghost")
    # Assert
    assert body is None


def test_take_snapshot_tolerates_corrupt_prev(cache_dir: Path, isolated_path) -> None:
    # Arrange: existing latest is malformed JSON.
    (cache_dir / "alpha.latest.json").write_text("{not json")
    # Act
    snap = mod.take_snapshot("alpha")
    # Assert
    assert snap["agent"] == "alpha"


# ---------------------------------------------------------------------------
# snapshot_tick
# ---------------------------------------------------------------------------


def test_snapshot_tick_smoke(cache_dir: Path, isolated_path) -> None:
    # Arrange
    # Act
    mod.snapshot_tick("alpha")
    # Assert
    assert (cache_dir / "alpha.latest.json").is_file()


def test_snapshot_tick_swallows_exceptions(tmp_path: Path, env_save_restore) -> None:
    """Daemon helper must swallow real errors so the loop survives.

    Point the cache dir at a path that exists as a *file* (not a dir);
    the runtime mkdir/open path inside take_snapshot raises, and
    snapshot_tick must not propagate it.
    """
    # Arrange: cache dir points at a regular file -> mkdir/open will fail.
    bad = tmp_path / "not_a_dir"
    bad.write_text("")
    env_save_restore.set("SAC_CACHE_DIR", str(bad))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CACHE_DIR", str(bad))
    # Act
    result = mod.snapshot_tick("alpha")
    # Assert: returns None (no exception propagated).
    assert result is None


# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_json(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "x.json"
    # Act
    mod._atomic_write_json(p, {"k": 1})
    # Assert
    assert json.loads(p.read_text()) == {"k": 1}
