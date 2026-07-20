"""The record rail: report the unhealable, record the recovery.

The record is the alternative to an infinite bounce — it must appear when an
agent is over the hourly cap, and the recovery must be recorded on its own the
moment the agent is no longer login-expired, or the log fills with stale
alarms nobody reads.

ABSENCE MEANS RECOVERY HERE, AND ONLY HERE. This pass reports only the agents
currently login-expired, so a remembered agent that is NOT in a later pass's
reports has recovered on its own — which is why this rail sweeps absent
subjects and a whole-fleet pass such as the reconciler must not.

PA-306: no ``unittest.mock``, no monkeypatching. A REAL temp JSONL event log
through the module's own ``path=`` seam; every assertion reads it back through
the production :func:`read_events`.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container._authheal._alarm import (
    SUBSYSTEM,
    record_pass_completed,
    record_reports,
)
from scitex_agent_container._authheal._pass import AgentReport
from scitex_agent_container._events import (
    PASS_COMPLETED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    read_events,
)
from scitex_agent_container._reconcile._rule import Verdict

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0


def _report(name: str, verdict: Verdict) -> AgentReport:
    return AgentReport(name=name, verdict=verdict, reason="t", detail="detail")


def _kinds(events: Path) -> list[str]:
    return [e.event for e in read_events(events, subsystem=SUBSYSTEM)]


def test_an_over_budget_report_records_a_degraded_event(events):
    # Arrange — an agent the restarter has given up on.
    reports = [_report("hpc", Verdict.OVER_BUDGET)]
    # Act
    record_reports(reports, path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_the_degraded_record_names_the_agent(events):
    # Arrange — never silent: a reader must see WHICH agent.
    # Act
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].subject == "hpc"


def test_the_degraded_record_says_restarting_stopped(events):
    # Arrange — the record must teach the reader what to conclude: a restart
    # LOOP is worse than a wedged agent, and the usual cause is an account
    # that cannot refresh, which no restart fixes.
    # Act
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Assert
    assert "stopped restarting it" in read_events(events)[0].detail


def test_a_restarted_report_records_the_recovery(events):
    # Arrange — first over budget (recorded), then successfully restarted.
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Act
    record_reports([_report("hpc", Verdict.RESTARTED)], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_an_agent_absent_from_a_later_pass_is_recovered(events):
    # Arrange — recorded degraded, then the agent recovers on its own (the
    # operator logged in) so it is no longer login-expired and never appears
    # in a later pass's reports at all.
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Act — a subsequent pass finds nothing wrong.
    record_reports([], path=events, now=NOW)
    # Assert — the stale alarm clears without a human.
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_the_swept_recovery_says_why_it_recovered(events):
    # Arrange — absence is the OBSERVATION here, and the record must say so,
    # or a reader cannot tell it from an observed clean reading.
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Act
    record_reports([], path=events, now=NOW)
    # Assert
    assert (
        "absent from this pass"
        in read_events(events, event=SUBJECT_RECOVERED)[0].detail
    )


def test_an_unobserved_agent_is_never_swept_as_recovered(events):
    # Arrange — an UNOBSERVED agent IS in the reports, so it must not be swept:
    # recording a recovery on a reading we never took would be a false
    # all-clear in its most durable form.
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Act
    record_reports([_report("hpc", Verdict.UNOBSERVED)], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_the_degraded_agent_is_reported_to_the_caller(events):
    # Arrange
    reports = [_report("hpc", Verdict.OVER_BUDGET)]
    # Act
    outcome = record_reports(reports, path=events, now=NOW)
    # Assert
    assert outcome.degraded == ("hpc",)


def test_the_swept_agent_is_reported_to_the_caller(events):
    # Arrange — the sweep's recoveries must reach the caller too, or the CLI
    # summary silently under-reports what the pass concluded.
    record_reports([_report("hpc", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Act
    outcome = record_reports([], path=events, now=NOW)
    # Assert
    assert outcome.recovered == ("hpc",)


def test_a_healthy_agent_without_prior_trouble_records_nothing(events):
    # Arrange — a restarted agent that was never over budget.
    # Act
    record_reports([_report("hpc", Verdict.RESTARTED)], path=events, now=NOW)
    # Assert
    assert not events.exists()


def test_the_pass_record_is_written_on_a_clean_pass(events):
    # Arrange — a pass that finds nothing wrong still leaves proof the timer
    # ticked; a rail that only writes during trouble cannot distinguish a
    # healthy fleet from a restarter that stopped running months ago.
    # Act
    record_pass_completed({}, mode="check", host="host-a", path=events, now=NOW)
    # Assert
    assert _kinds(events) == [PASS_COMPLETED]


def test_the_pass_record_carries_the_mode(events):
    # Arrange — a hand-run --check also writes this record.
    # Act
    record_pass_completed({}, mode="check", path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["mode"] == "check"


def test_the_pass_record_carries_the_counts(events):
    # Arrange
    # Act
    record_pass_completed({"RESTARTED": 2}, mode="apply", path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw["counts"] == {"RESTARTED": 2}


def test_a_recording_failure_does_not_raise(events, unwritable):
    # Arrange — recording is a SIDE rail and must never crash the restart
    # pass that feeds it.
    # Act
    outcome = record_reports(
        [_report("hpc", Verdict.OVER_BUDGET)],
        path=unwritable,
        now=NOW,
        err_stream=io.StringIO(),
    )
    # Assert
    assert outcome.failed == ("hpc",)


def test_a_recording_failure_is_loud(events, unwritable):
    # Arrange
    stream = io.StringIO()
    # Act
    record_reports(
        [_report("hpc", Verdict.OVER_BUDGET)],
        path=unwritable,
        now=NOW,
        err_stream=stream,
    )
    # Assert
    assert "FAILED to record" in stream.getvalue()
