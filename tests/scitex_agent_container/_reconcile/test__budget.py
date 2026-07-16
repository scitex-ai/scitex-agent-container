"""Tests for ``_reconcile._budget`` — a restart loop is worse than a down agent.

Real temp files, a real injected clock (the module takes ``now`` as a
parameter — there is no clock to mock), no mocks.

The behaviours that matter:

* the 30-minute debounce blocks a second restart of the same agent — which
  is also the boot grace: an agent restarted 3 minutes ago is still coming
  up, and bouncing it again would kill the recovery in progress;
* the hourly cap is reachable on its OWN (restarts at T-3500s and T-1900s
  clear the debounce but not the hour) — that is the leg that turns a
  persistently-dying agent into a board card instead of a forever-bounce;
* the pass cap bounds the blast radius of ONE bad tick;
* the history survives the process, because the pass is a short-lived cron
  and an in-RAM memory is exactly how ``health_monitor``'s retry counter
  evaporated.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._reconcile._budget import (
    DEBOUNCE_S,
    MAX_RESTARTS_PER_AGENT_PER_HOUR,
    Budget,
    HistoryState,
    history_path,
    load_history,
    read_history,
    save_history,
)

_NOW = 1_800_000_000.0

#: Two restarts inside the hour whose LATEST is past the 30min debounce —
#: the only shape that reaches the hourly cap on its own.
_AGES_PAST_DEBOUNCE = (3_500, 1_900)


# --- the debounce -----------------------------------------------------------


def test_fresh_agent_is_within_budget():
    # Arrange — never restarted before.
    budget = Budget({})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.allowed


def test_debounce_blocks_a_second_restart_inside_the_window():
    # Arrange — restarted 10 minutes ago; the debounce is 30.
    budget = Budget({"alpha": [_NOW - 600]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert not check.allowed


def test_debounce_names_itself_as_the_blocker():
    # Arrange — the operator must be able to tell a debounce from a cap.
    budget = Budget({"alpha": [_NOW - 600]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.reason == "debounce"


def test_restart_allowed_once_debounce_elapsed():
    # Arrange — one restart, just over the debounce ago, so the hourly
    # count (1) is still under the cap.
    budget = Budget({"alpha": [_NOW - (DEBOUNCE_S + 60)]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.allowed


def test_debounce_only_binds_the_same_agent():
    # Arrange — alpha was just restarted; beta is a different agent and
    # must not inherit alpha's cooldown.
    budget = Budget({"alpha": [_NOW - 60]})
    # Act
    check = budget.check("beta", _NOW)
    # Assert
    assert check.allowed


# --- the hourly cap: reachable on its own, and the card trigger -------------


def test_two_restarts_in_the_hour_exhausts_the_agent():
    # Arrange — restarts at T-3500s and T-1900s. The debounce PASSES (1900s
    # > 1800s), so only the hourly cap can catch this. Restarting a third
    # time would be bouncing an agent that is plainly not being fixed.
    budget = Budget({"alpha": [_NOW - 3_500, _NOW - 1_900]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert not check.allowed


def test_hourly_cap_reports_over_budget():
    # Arrange — over-budget is the verdict that raises a board card, so the
    # reason must be distinguishable from a plain debounce.
    budget = Budget({"alpha": [_NOW - 3_500, _NOW - 1_900]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.reason == "over-budget"


def test_restarts_older_than_an_hour_are_forgotten():
    # Arrange — two restarts, both >1h ago. A sliding window must not
    # condemn an agent forever for having crashed last week.
    budget = Budget({"alpha": [_NOW - 7_200, _NOW - 3_700]})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.allowed


def test_hourly_cap_matches_the_documented_constant():
    # Arrange — the cap the module advertises is the cap it enforces.
    stamps = [_NOW - 3_500 - i for i in range(MAX_RESTARTS_PER_AGENT_PER_HOUR)]
    budget = Budget({"alpha": stamps})
    # Act
    check = budget.check("alpha", _NOW)
    # Assert
    assert check.reason == "over-budget"


# --- the per-pass cap: blast radius of ONE bad tick -------------------------


def test_pass_cap_stops_a_single_tick_storming():
    # Arrange — a cap of 2, already spent. If a tmux hiccup ever made the
    # fleet look dead, this is the difference between 2 and 93 restarts.
    budget = Budget({}, pass_cap=2)
    budget.record("a", _NOW)
    budget.record("b", _NOW)
    # Act
    check = budget.check("c", _NOW)
    # Assert
    assert not check.allowed


def test_pass_cap_is_reported_as_its_own_reason():
    # Arrange — CAPPED is deferred-not-lost, so it must be distinguishable
    # from OVER-BUDGET (which is carded and needs a human).
    budget = Budget({}, pass_cap=1)
    budget.record("a", _NOW)
    # Act
    check = budget.check("b", _NOW)
    # Assert
    assert check.reason == "pass-cap"


def test_recording_a_restart_spends_pass_budget():
    # Arrange
    budget = Budget({}, pass_cap=5)
    # Act
    budget.record("alpha", _NOW)
    # Assert
    assert budget.spent == 1


# --- persistence: the pass is short-lived, its memory must not be -----------


def test_history_survives_a_save_and_load(tmp_path: Path):
    # Arrange — the cron process dies every pass; the debounce must not.
    path = tmp_path / "hist.json"
    # Act
    save_history(path, {"alpha": [_NOW - 60]}, now=_NOW)
    # Assert
    assert load_history(path) == {"alpha": [_NOW - 60]}


def test_missing_history_file_is_an_empty_memory(tmp_path: Path):
    # Arrange — the FIRST run ever has no file. That is normal, not an error.
    # Act
    history = load_history(tmp_path / "nope.json")
    # Assert
    assert history == {}


# --- DENIED is not MISSING: the read is three-state --------------------------
#
# `except OSError: return {}` would make "forbidden to read" identical to
# "nothing restarted yet". Since this file is the ONLY memory of what we have
# restarted, that collapse silently disarms BOTH rate limits on every tick —
# turning the enforcer into the restart loop it exists to prevent, over one
# permission error. Measured precedent (Spartan, 2026-07-16): ~/.scitex is a
# SYMLINK into a project whose membership was revoked, so every $HOME-resolved
# path under it became permission-denied while still LOOKING configured.


def test_first_run_is_distinct_from_denied(tmp_path: Path):
    # Arrange — a genuinely absent history in a writable dir.
    # Act
    read = read_history(tmp_path / "hist.json")
    # Assert
    assert read.state is HistoryState.FIRST_RUN


def test_first_run_is_enforceable(tmp_path: Path):
    # Arrange — absent-and-creatable really does mean nothing restarted yet.
    # Act
    read = read_history(tmp_path / "hist.json")
    # Assert
    assert read.enforceable


def test_first_run_proves_writability_by_writing(tmp_path: Path):
    # Arrange — FileNotFoundError is ambiguous (never written vs the tree is
    # gone), so we PROVE we can create it rather than assume. The probe IS
    # the operation — the only proof that cannot be stale.
    path = tmp_path / "deep" / "hist.json"
    # Act
    read_history(path)
    # Assert
    assert path.exists()


def test_unreadable_dir_is_denied_not_a_first_run(tmp_path: Path):
    # Arrange — THE Spartan case: the state root cannot be read or written.
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        read = read_history(denied / "hist.json")
        # Assert
        assert read.state is HistoryState.DENIED
    finally:
        denied.chmod(0o755)


def test_denied_history_is_not_enforceable(tmp_path: Path):
    # Arrange — an unenforceable budget is not a budget: with no memory,
    # every corpse is restartable on every 5-minute tick, forever.
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        read = read_history(denied / "hist.json")
        # Assert
        assert not read.enforceable
    finally:
        denied.chmod(0o755)


def test_denied_history_says_why(tmp_path: Path):
    # Arrange — a refusal nobody can diagnose is a no-op with extra steps.
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        read = read_history(denied / "hist.json")
        # Assert
        assert read.detail.strip()
    finally:
        denied.chmod(0o755)


def test_unreadable_file_is_denied_not_empty(tmp_path: Path):
    # Arrange — the file EXISTS but is not ours to read. The memory is
    # there; we simply may not see it. That is the opposite of empty.
    path = tmp_path / "hist.json"
    path.write_text('{"alpha": [1.0]}')
    path.chmod(0o000)
    try:
        # Act
        read = read_history(path)
        # Assert
        assert read.state is HistoryState.DENIED
    finally:
        path.chmod(0o644)


def test_corrupt_history_is_unreadable_not_empty(tmp_path: Path):
    # Arrange — a garbled file means we demonstrably HAVE a restart memory
    # and cannot parse it. Treating that as "nothing restarted" would disarm
    # the budget just as thoroughly as a permission error.
    path = tmp_path / "hist.json"
    path.write_text("{not json at all")
    # Act
    read = read_history(path)
    # Assert
    assert read.state is HistoryState.UNREADABLE


def test_corrupt_history_is_not_enforceable(tmp_path: Path):
    # Arrange
    path = tmp_path / "hist.json"
    path.write_text("{not json at all")
    # Act
    read = read_history(path)
    # Assert
    assert not read.enforceable


def test_non_object_history_is_unreadable(tmp_path: Path):
    # Arrange — valid JSON, wrong shape. Refuse to guess.
    path = tmp_path / "hist.json"
    path.write_text("[1, 2, 3]")
    # Act
    read = read_history(path)
    # Assert
    assert read.state is HistoryState.UNREADABLE


def test_readable_history_is_enforceable(tmp_path: Path):
    # Arrange — the happy path still works.
    path = tmp_path / "hist.json"
    path.write_text(f'{{"alpha": [{_NOW}]}}')
    # Act
    read = read_history(path)
    # Assert
    assert read.enforceable


def test_reading_a_real_history_returns_it(tmp_path: Path):
    # Arrange
    path = tmp_path / "hist.json"
    path.write_text(f'{{"alpha": [{_NOW}]}}')
    # Act
    read = read_history(path)
    # Assert
    assert read.history == {"alpha": [_NOW]}


def test_history_path_honours_the_pin_override(tmp_path: Path, env_save_restore):
    # Arrange — the state must be pinnable into a failure domain that does
    # NOT die with the thing it watches. The default lives under $SCITEX_DIR,
    # which on at least one host is a symlink into a revocable project.
    env_save_restore.set("SAC_RECONCILE_HISTORY", str(tmp_path / "pinned.json"))
    # Act
    resolved = history_path()
    # Assert
    assert resolved == tmp_path / "pinned.json"


def test_saving_prunes_stamps_beyond_the_window(tmp_path: Path):
    # Arrange — an ancient restart is outside every horizon this module
    # reasons over, so it must not accumulate forever.
    path = tmp_path / "hist.json"
    # Act
    save_history(path, {"alpha": [_NOW - 99_999, _NOW - 60]}, now=_NOW)
    # Assert
    assert load_history(path) == {"alpha": [_NOW - 60]}


def test_saving_drops_an_agent_with_no_recent_restarts(tmp_path: Path):
    # Arrange — every stamp is ancient, so the agent's whole entry goes.
    path = tmp_path / "hist.json"
    # Act
    save_history(path, {"alpha": [_NOW - 99_999]}, now=_NOW)
    # Assert
    assert load_history(path) == {}


def test_save_creates_the_runtime_dir(tmp_path: Path):
    # Arrange — the first ever run may predate the runtime dir.
    path = tmp_path / "fresh" / "deep" / "hist.json"
    # Act
    save_history(path, {"alpha": [_NOW]}, now=_NOW)
    # Assert
    assert path.exists()


def test_save_leaves_no_temp_file_behind(tmp_path: Path):
    # Arrange — the write is atomic (tmp+replace); a leftover .tmp would
    # mean a partial write could be read back as the real history.
    path = tmp_path / "hist.json"
    # Act
    save_history(path, {"alpha": [_NOW]}, now=_NOW)
    # Assert
    assert list(tmp_path.glob("*.tmp")) == []
