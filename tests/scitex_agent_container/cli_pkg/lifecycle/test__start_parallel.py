"""Tests for the serialized multi-start queue (sac-multi-start-queue-oauth).

No mocks / no monkeypatch: the bounded-parallel launcher is exercised
against a REAL ``sac`` shim script placed on ``$PATH`` via a yield
fixture that mutates the real ``os.environ`` and restores it. The shim
is a tiny bash script that records each invocation (timestamp + target)
to a shared log file and exits with a target-dependent code, so the
tests can assert the concurrency cap, the stagger spacing, that every
target was attempted, and the non-zero exit on a failed target — all
from the real subprocess fan-out, not a substituted function.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._start_parallel import (
    build_child_argv,
    maybe_run_parallel,
    run_parallel_targets,
)


@pytest.fixture
def sac_shim(tmp_path):
    """Install a real ``sac`` shim on ``$PATH``; yield its invocation log.

    The shim appends ``<epoch_ms> <target>`` to ``$SAC_SHIM_LOG`` on each
    call and sleeps briefly so overlapping (concurrent) launches are
    observable in the timestamps. A target literally named ``boom`` makes
    the shim exit 7 — the failure path. The yield restores ``os.environ``
    (real, no monkeypatch).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    shim = bindir / "sac"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# args: agents start <target> --yes --no-redispatch ...\n"
        'target="$3"\n'
        'printf "%s %s\\n" "$(date +%s%3N)" "$target" >> "$SAC_SHIM_LOG"\n'
        "sleep 0.5\n"
        'if [ "$target" = "boom" ]; then exit 7; fi\n'
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    saved_path = os.environ.get("PATH", "")
    saved_log = os.environ.get("SAC_SHIM_LOG")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{saved_path}"
    os.environ["SAC_SHIM_LOG"] = str(log)
    try:
        yield log
    finally:
        os.environ["PATH"] = saved_path
        if saved_log is None:
            os.environ.pop("SAC_SHIM_LOG", None)
        else:
            os.environ["SAC_SHIM_LOG"] = saved_log


def _read_calls(log: Path) -> list[tuple[int, str]]:
    """Parse the shim log into ``(epoch_ms, target)`` rows."""
    rows = []
    for line in log.read_text().splitlines():
        ts, target = line.split(" ", 1)
        rows.append((int(ts), target.strip()))
    return rows


def _kwargs(**over):
    """Default keyword args for ``run_parallel_targets``; override as needed."""
    base = dict(
        concurrency=3,
        stagger=0.0,
        no_preflight=False,
        force=False,
        session_mode=None,
        strict_drift=False,
        broker_self=False,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# build_child_argv — pure argv assembly (no subprocess).
# ---------------------------------------------------------------------------


def _argv(**over):
    """Default keyword args for ``build_child_argv``; override as needed."""
    base = dict(
        sac_bin="sac",
        no_preflight=False,
        force=False,
        session_mode=None,
        strict_drift=False,
        broker_self=False,
    )
    base.update(over)
    return build_child_argv("alpha", **base)


class TestBuildChildArgv:
    def test_always_includes_yes_flag(self):
        # Arrange
        # (defaults supplied by _argv)
        # Act
        argv = _argv()
        # Assert
        assert "--yes" in argv

    def test_always_includes_no_redispatch_flag(self):
        # Arrange
        # (defaults supplied by _argv)
        # Act
        argv = _argv()
        # Assert
        assert "--no-redispatch" in argv

    def test_target_is_present_as_positional(self):
        # Arrange
        # (defaults supplied by _argv)
        # Act
        argv = _argv()
        # Assert
        assert "alpha" in argv

    def test_force_flag_propagated_when_set(self):
        # Arrange
        # (force toggled on below)
        # Act
        argv = _argv(force=True)
        # Assert
        assert "--force" in argv

    def test_session_mode_propagated_when_set(self):
        # Arrange
        # (session_mode supplied below)
        # Act
        argv = _argv(session_mode="continue")
        # Assert
        assert "continue" in argv

    def test_profile_propagated_when_set(self):
        # Arrange
        # (profile supplied below)
        # Act
        argv = _argv(profile="codex")
        # Assert
        assert argv[-2:] == ["--profile", "codex"]

    def test_no_preflight_absent_when_unset(self):
        # Arrange
        # (defaults supplied by _argv)
        # Act
        argv = _argv()
        # Assert
        assert "--no-preflight" not in argv


# ---------------------------------------------------------------------------
# run_parallel_targets — real subprocess fan-out against the sac shim.
# ---------------------------------------------------------------------------


class TestRunParallelTargets:
    def test_all_targets_attempted(self, sac_shim):
        # Arrange
        targets = ["a", "b", "c", "d"]
        # Act
        run_parallel_targets(targets, **_kwargs())
        # Assert
        attempted = {t for _, t in _read_calls(sac_shim)}
        assert attempted == set(targets)

    def test_concurrency_cap_respected(self, sac_shim):
        # Arrange — cap=2 over 4 targets, each child sleeping 0.5s, forces
        # TWO serial waves ({a,b} then {c,d}), so the whole call takes
        # >= ~1.0s; an unbounded pool would finish the single wave in ~0.5s.
        targets = ["a", "b", "c", "d"]
        # Act — assert the wall-clock FLOOR, not per-child exec-time gaps.
        # The floor is load-independent (load only makes children slower, so
        # elapsed only grows); the old starts[2]-starts[0] exec-gap flaked in
        # the CI SIF when a cold first child started slower than a warm third.
        t0 = time.monotonic()
        run_parallel_targets(targets, **_kwargs(concurrency=2, stagger=0.0))
        elapsed = time.monotonic() - t0
        # Assert — two waves of a 0.5s child => >= ~1.0s (0.9s tolerance).
        assert elapsed >= 0.9

    def test_stagger_spacing_applied(self, sac_shim):
        # Arrange — stagger=0.3s between submissions. run_parallel_targets
        # sleeps `stagger` in the MAIN thread between each pool.submit, so the
        # whole call takes AT LEAST (N-1)*stagger regardless of child exec
        # latency. That floor is load-independent (load only adds); measuring
        # the per-child exec-time gap instead (the old starts[1]-starts[0])
        # flaked in the CI SIF when a cold first child ran slower than a warm
        # second, compressing the measured gap below the real submission spacing.
        targets = ["a", "b", "c"]
        stagger = 0.3
        # Act
        t0 = time.monotonic()
        run_parallel_targets(targets, **_kwargs(concurrency=3, stagger=stagger))
        elapsed = time.monotonic() - t0
        # Assert — cumulative stagger floor: (N-1) sleeps of `stagger`.
        assert elapsed >= (len(targets) - 1) * stagger - 0.05

    def test_nonzero_exit_when_a_target_fails(self, sac_shim):
        # Arrange — the ``boom`` target makes the shim exit 7; capture the
        # SystemExit code the launcher raises on any failed child.
        targets = ["a", "boom", "c"]
        code = None
        # Act
        try:
            run_parallel_targets(targets, **_kwargs())
        except SystemExit as exc:
            code = exc.code
        # Assert
        assert code == 1

    def test_clean_exit_when_all_succeed(self, sac_shim):
        # Arrange
        targets = ["a", "b"]
        # Act — no SystemExit raised on the all-green path.
        run_parallel_targets(targets, **_kwargs())
        # Assert — reaching here without exception is the contract.
        assert sac_shim.exists()


# ---------------------------------------------------------------------------
# maybe_run_parallel — routing decision. ``preflight_runner`` is a real
# no-op callable (a sentinel list whose mutation we never need); the
# sac_shim fixture supplies the child subprocess so the multi-target
# branch can actually run.
# ---------------------------------------------------------------------------


def _route_kwargs(**over):
    """Default keyword args for ``maybe_run_parallel``; override as needed."""
    base = dict(
        single_targets=[],
        bulk_yamls=[],
        concurrency=3,
        stagger=0.0,
        yes=True,
        no_preflight=False,
        force=False,
        session_mode=None,
        strict_drift=False,
        broker_self=False,
        foreground=False,
        multi_foreground=False,
        one_shot=False,
        resume_id=None,
        dry_run=False,
        as_json=False,
        preflight_runner=lambda: None,
    )
    base.update(over)
    return base


class TestMaybeRunParallelRouting:
    def test_single_target_does_not_route(self):
        # Arrange
        kwargs = _route_kwargs(single_targets=["only"])
        # Act
        handled = maybe_run_parallel(**kwargs)
        # Assert
        assert handled is False

    def test_dry_run_multi_target_does_not_route(self):
        # Arrange
        kwargs = _route_kwargs(single_targets=["a", "b"], dry_run=True)
        # Act
        handled = maybe_run_parallel(**kwargs)
        # Assert
        assert handled is False

    def test_foreground_multi_target_does_not_route(self):
        # Arrange
        kwargs = _route_kwargs(single_targets=["a", "b"], foreground=True)
        # Act
        handled = maybe_run_parallel(**kwargs)
        # Assert
        assert handled is False

    def test_resume_multi_target_does_not_route(self):
        # Arrange
        kwargs = _route_kwargs(single_targets=["a", "b"], resume_id="uuid")
        # Act
        handled = maybe_run_parallel(**kwargs)
        # Assert
        assert handled is False

    def test_multi_target_routes_and_returns_true(self, sac_shim):
        # Arrange
        kwargs = _route_kwargs(single_targets=["a", "b"])
        # Act
        handled = maybe_run_parallel(**kwargs)
        # Assert
        assert handled is True

    def test_bulk_multi_without_yes_exits_two(self):
        # Arrange — a bulk-dir multi-launch still requires --yes.
        kwargs = _route_kwargs(bulk_yamls=["x/x.yaml", "y/y.yaml"], yes=False)
        code = None
        # Act
        try:
            maybe_run_parallel(**kwargs)
        except SystemExit as exc:
            code = exc.code
        # Assert
        assert code == 2
