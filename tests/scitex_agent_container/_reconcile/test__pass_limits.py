"""``_reconcile._pass`` — the guards that stop a helper becoming a hazard.

Sibling of ``test__pass.py`` (which owns WHICH agents get restarted); this
file owns what happens when the answer is "too many, too often, or not
working": rate limits, restart failures, exit codes, and the record rails.

Same no-mocks setup — real temp ``state.db``, real specs, a real temp sac
event log, real injected clock. Fixtures in ``conftest.py``, helpers in
``_fleet.py``.

The behaviours that matter:

* the debounce and the hourly cap stop a restart LOOP, which is worse than
  a down agent — it never converges and buries the real cause;
* COOLING-DOWN (inside the debounce) must NOT record, or every healthy heal
  leaves a record on the next 5-minute tick and the log becomes noise;
* OVER-BUDGET must record: giving up SILENTLY is the original bug with
  extra steps;
* recording is a SIDE rail — a write failure can never unwind, block or
  rewrite what the pass did to the fleet.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io

from scitex_agent_container._events import (
    PASS_COMPLETED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    read_events,
)
from scitex_agent_container._reconcile._alarm import SUBSYSTEM
from scitex_agent_container._reconcile._budget import DEBOUNCE_S
from scitex_agent_container._reconcile._rule import Verdict
from tests.scitex_agent_container._reconcile._fleet import (
    NOW,
    Recorder,
    ghost,
    run_pass,
    sessions,
    verdict_of,
    write_spec,
)

import pytest


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


def _subjects(events, kind: str) -> list[str]:
    """Subjects this pass recorded under ``kind``, in order."""
    return [
        e.subject
        for e in read_events(events, subsystem=SUBSYSTEM, event=kind)
    ]


def _pass_records(events) -> list:
    """Every pass-completed record this suite's run(s) left behind."""
    return read_events(events, subsystem=SUBSYSTEM, event=PASS_COMPLETED)


# --- the debounce: a healthy recovery in progress ---------------------------


def test_debounce_blocks_a_second_restart(registry, db_path, history, events):
    # Arrange — restarted 10 min ago (debounce is 30). It is either still
    # booting or something is killing it faster than we can fix it.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 600}]}}')
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, events, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


def test_debounced_agent_reports_cooling_down(registry, db_path, history, events):
    # Arrange — a recovery IN PROGRESS, not a failure: it must be
    # distinguishable from "restarting is not fixing it".
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 600}]}}')
    # Act
    outcome = run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.COOLING_DOWN


def test_debounced_agent_records_no_verdict(registry, db_path, history, events):
    # Arrange — THE log-spam guard. The debounce is 30min and the timer
    # ticks every 5min, so a perfectly HEALTHY restart sits inside its own
    # debounce for the next five ticks. If that recorded, every successful
    # heal would leave a degraded record and a reader would learn to ignore
    # the log — which is how the fleet died unnoticed in the first place.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 600}]}}')
    # Act
    run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert _subjects(events, SUBJECT_DEGRADED) == []


def test_second_pass_inside_debounce_does_not_rebounce(
    registry, db_path, history, events
):
    # Arrange — a full pass restarts alpha and persists the history.
    write_spec(registry, "alpha")
    ghost("alpha")
    run_pass(registry, db_path, history, events, apply=True)
    recorder = Recorder()
    # Act — the timer fires again 5 minutes later, agent still down.
    run_pass(
        registry,
        db_path,
        history,
        events,
        apply=True,
        now=NOW + 300,
        restart_fn=recorder,
    )
    # Assert — the memory survived the first process; no bounce loop.
    assert recorder.names == []


def test_restart_resumes_after_the_debounce_elapses(registry, db_path, history, events):
    # Arrange — one restart, longer ago than the debounce.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - DEBOUNCE_S - 60}]}}')
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, events, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == ["alpha"]


# --- the hourly cap: restarting is NOT fixing this --------------------------


def test_over_budget_agent_is_not_restarted(registry, db_path, history, events):
    # Arrange — 2 restarts inside the hour, the latest past the debounce.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 3_500}, {NOW - 1_900}]}}')
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, events, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


def test_over_budget_agent_is_recorded_degraded(registry, db_path, history, events):
    # Arrange — giving up SILENTLY is the original bug with extra steps.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 3_500}, {NOW - 1_900}]}}')
    # Act
    run_pass(registry, db_path, history, events, apply=True)
    # Assert — it reaches sac's own log as a degraded record, naming alpha.
    assert _subjects(events, SUBJECT_DEGRADED) == ["alpha"]


def test_over_budget_verdict_is_reported(registry, db_path, history, events):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text(f'{{"alpha": [{NOW - 3_500}, {NOW - 1_900}]}}')
    # Act
    outcome = run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.OVER_BUDGET


# --- the per-pass cap: blast radius of ONE bad tick -------------------------


def test_pass_limit_caps_one_tick(registry, db_path, history, events):
    # Arrange — 5 corpses but a limit of 2. If a tmux hiccup ever made the
    # fleet look dead, this is what stops a 93-restart storm.
    for name in ("a1", "a2", "a3", "a4", "a5"):
        write_spec(registry, name)
        ghost(name)
    recorder = Recorder()
    # Act
    run_pass(
        registry, db_path, history, events, apply=True, limit=2, restart_fn=recorder
    )
    # Assert
    assert len(recorder.names) == 2


def test_capped_agents_are_reported_not_dropped(registry, db_path, history, events):
    # Arrange — the remainder is DEFERRED, not lost, and must be visible.
    for name in ("a1", "a2", "a3"):
        write_spec(registry, name)
        ghost(name)
    # Act
    outcome = run_pass(registry, db_path, history, events, apply=True, limit=1)
    # Assert
    assert len(outcome.of(Verdict.CAPPED)) == 2


def test_capped_agent_gets_no_record(registry, db_path, history, events):
    # Arrange — CAPPED is retried in 5 minutes; recording it would be noise.
    for name in ("a1", "a2"):
        write_spec(registry, name)
        ghost(name)
    # Act
    run_pass(registry, db_path, history, events, apply=True, limit=1)
    # Assert
    assert _subjects(events, SUBJECT_DEGRADED) == []


def test_history_is_persisted_per_restart(registry, db_path, history, events):
    # Arrange — the scheduled form runs under a systemd timeout. A pass
    # killed mid-sweep must still remember what it already bounced, or the
    # next tick re-bounces it with the debounce silently disarmed.
    for name in ("a1", "a2"):
        write_spec(registry, name)
        ghost(name)
    # Act — a restart_fn that dies partway through, like a SIGKILL would.
    run_pass(
        registry,
        db_path,
        history,
        events,
        apply=True,
        restart_fn=Recorder(boom=RuntimeError("host died mid-pass")),
    )
    # Assert — the agents it reached are on disk despite the failures.
    assert "a1" in history.read_text()


# --- restart failures --------------------------------------------------------


def test_failed_restart_is_reported_failed(registry, db_path, history, events):
    # Arrange — the restart ran but reported failure; the agent is still down.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(
        registry, db_path, history, events, apply=True, restart_fn=Recorder(ok=False)
    )
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.FAILED


def test_raising_restart_does_not_abort_the_sweep(registry, db_path, history, events):
    # Arrange — one agent's restart raising must not strand the others: the
    # rest of the fleet is still down and still needs recovering.
    for name in ("a1", "a2"):
        write_spec(registry, name)
        ghost(name)
    recorder = Recorder(boom=RuntimeError("tmux refused"))
    # Act
    run_pass(registry, db_path, history, events, apply=True, restart_fn=recorder)
    # Assert — it tried BOTH.
    assert recorder.names == ["a1", "a2"]


def test_failed_restart_is_recorded_degraded(registry, db_path, history, events):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(
        registry, db_path, history, events, apply=True, restart_fn=Recorder(ok=False)
    )
    # Assert
    assert _subjects(events, SUBJECT_DEGRADED) == ["alpha"]


def test_recovered_agent_records_its_recovery(registry, db_path, history, events):
    # Arrange — alpha failed once, so a degraded record exists.
    write_spec(registry, "alpha")
    ghost("alpha")
    run_pass(
        registry, db_path, history, events, apply=True, restart_fn=Recorder(ok=False)
    )
    # Act — later, alpha is alive again (a fixed problem must stop shouting).
    run_pass(
        registry,
        db_path,
        history,
        events,
        apply=True,
        now=NOW + 99_999,
        snapshot_fn=lambda **_: sessions("alpha"),
    )
    # Assert
    assert _subjects(events, SUBJECT_RECOVERED) == ["alpha"]


# --- exit codes -------------------------------------------------------------


def test_healthy_fleet_exits_clean(registry, db_path, history, events):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(
        registry, db_path, history, events, snapshot_fn=lambda **_: sessions("alpha")
    )
    # Assert
    assert outcome.exit_code() == 0


def test_down_agent_exits_nonzero(registry, db_path, history, events):
    # Arrange — cron-friendly: a dry run detecting a corpse must exit != 0.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(registry, db_path, history, events)
    # Assert
    assert outcome.exit_code() == 1


def test_blind_pass_exits_two(registry, db_path, history, events):
    # Arrange — a pass that could not see the fleet must NOT exit 0 and let
    # a cron log it as a healthy tick. Unknown is not clean.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(registry, db_path, history, events, snapshot_fn=lambda **_: None)
    # Assert
    assert outcome.exit_code() == 2


def test_successful_recovery_exits_clean(registry, db_path, history, events):
    # Arrange — a pass that healed the fleet did its job; that is a success.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert outcome.exit_code() == 0


# --- the pass record rides every pass --------------------------------------


def test_clean_pass_still_records_the_pass(registry, db_path, history, events):
    # Arrange — "0 restarted, all healthy" is the MOST important tick: a
    # beacon that only appears during trouble cannot prove it is alive.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(
        registry,
        db_path,
        history,
        events,
        apply=True,
        snapshot_fn=lambda **_: sessions("alpha"),
    )
    # Assert
    assert len(_pass_records(events)) == 1


def test_empty_fleet_still_records_the_pass(registry, db_path, history, events):
    # Arrange — nothing to do at all is still proof the mechanism ran.
    # Act
    run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert len(_pass_records(events)) == 1


def test_dry_run_still_records_the_pass(registry, db_path, history, events):
    # Arrange — the beacon is about the RECONCILER, not about an agent, so
    # it ticks in both modes (the record's ``mode`` says which).
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(registry, db_path, history, events)
    # Assert
    assert len(_pass_records(events)) == 1


def test_the_pass_record_is_reported_to_the_caller(registry, db_path, history, events):
    # Arrange
    # Act
    outcome = run_pass(registry, db_path, history, events, apply=True)
    # Assert
    assert outcome.heartbeat_ok


# --- recording is a SIDE rail: it can never take the pass down -------------


def test_a_record_write_failure_still_restarts(registry, db_path, history, unwritable):
    # Arrange — an unwritable event log (read-only parent), so the REAL
    # event-log writer genuinely fails. No mocks: the world says no.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder(ok=False)
    # Act
    run_pass(
        registry,
        db_path,
        history,
        unwritable,
        apply=True,
        restart_fn=recorder,
        err_stream=io.StringIO(),
    )
    # Assert — the restart still happened; the recording rail is secondary.
    assert recorder.names == ["alpha"]


def test_a_record_write_failure_is_loud(registry, db_path, history, unwritable):
    # Arrange — a rail that fails silently is how the fleet died unnoticed.
    write_spec(registry, "alpha")
    ghost("alpha")
    stream = io.StringIO()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        unwritable,
        apply=True,
        restart_fn=Recorder(ok=False),
        err_stream=stream,
    )
    # Assert
    assert "FAILED" in stream.getvalue()


def test_a_record_write_failure_keeps_the_verdict(registry, db_path, history, unwritable):
    # Arrange — a recording failure must not rewrite what we concluded.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(
        registry,
        db_path,
        history,
        unwritable,
        apply=True,
        restart_fn=Recorder(ok=False),
        err_stream=io.StringIO(),
    )
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.FAILED


def test_a_pass_record_failure_does_not_stop_restarts(registry, db_path, history, unwritable):
    # Arrange — the beacon failing must not cost the fleet its recovery.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        unwritable,
        apply=True,
        restart_fn=recorder,
        err_stream=io.StringIO(),
    )
    # Assert
    assert recorder.names == ["alpha"]
