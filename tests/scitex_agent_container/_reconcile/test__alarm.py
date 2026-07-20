"""Tests for ``_reconcile._alarm`` — down records, the pass beat, self-impairment.

PA-306: no ``unittest.mock``, no monkeypatching. A REAL temporary JSONL event
log (``tmp_path/sac-events.jsonl``) passed through each function's own
``path=`` seam, and every assertion reads the bytes back through the
production :func:`read_events`. The fail-loud leg breaks the write
ORGANICALLY (a read-only parent dir) rather than injecting a raiser.

The behaviours that matter:

* an agent we could NOT recover gets a DEGRADED record naming it, and an
  agent that came back gets exactly ONE recovery record — an enforcer that
  gives up silently is just the original bug with extra steps;
* verdicts that resolve themselves (COOLING-DOWN, CAPPED, UNKNOWN, SKIPPED)
  get NO per-agent record, because a record per healthy heal on a 5-minute
  timer trains its reader to ignore the log;
* the pass record is written on EVERY pass, above all on the ones that found
  nothing wrong: "0 restarted, all healthy" is the only thing distinguishing
  a healthy fleet from a timer that stopped running months ago;
* a reconciler that cannot read its OWN restart memory says so, and says so
  again when it can — a refusal nobody hears is a no-op;
* every rail is a SIDE rail: a write failure prints loud and never raises.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container._events import (
    PASS_COMPLETED,
    SELF_IMPAIRED,
    SELF_RECOVERED,
    SUBJECT_DEGRADED,
    SUBJECT_RECOVERED,
    read_events,
)
from scitex_agent_container._reconcile._alarm import (
    SUBSYSTEM,
    record_pass_completed,
    record_reports,
    record_self_impaired,
    record_self_recovered,
)
from scitex_agent_container._reconcile._pass import AgentReport
from scitex_agent_container._reconcile._rule import Verdict

#: A fixed clock, so no test can be flaky on time.
NOW = 1_800_000_000.0

# ``events`` and ``unwritable`` come from conftest.py — a real temp event log
# and a genuinely read-only one, shared with the _pass suites.


def _report(name: str, verdict: Verdict) -> AgentReport:
    return AgentReport(
        name=name,
        verdict=verdict,
        reason="ghost-active-row",
        detail=f"{name} died and sac could not bring it back",
    )


def _kinds(events: Path) -> list[str]:
    return [e.event for e in read_events(events, subsystem=SUBSYSTEM)]


# --- down records -----------------------------------------------------------


def test_an_over_budget_agent_is_recorded_degraded(events):
    # Arrange — an agent restarting has plainly not fixed.
    # Act
    record_reports([_report("alpha", Verdict.OVER_BUDGET)], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_a_failed_agent_is_recorded_degraded(events):
    # Arrange — we tried and it did not come back.
    # Act
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED]


def test_the_down_record_names_the_agent(events):
    # Arrange — never silent: a reader must see WHICH agent.
    # Act
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].subject == "alpha"


def test_the_down_record_keeps_the_pass_verdict(events):
    # Arrange — the pass's own token, verbatim, so the record can be compared
    # against the code that produced it.
    # Act
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert
    assert read_events(events)[0].verdict == Verdict.FAILED.value


def test_a_second_down_run_records_again(events):
    # Arrange — the timer fires every 5 minutes and the agent is still down.
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Act
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert — an ongoing problem is an ongoing fact.
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_DEGRADED]


def test_a_recovered_agent_records_one_recovery(events):
    # Arrange — alpha was down, so a degraded record exists.
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Act — alpha is back.
    record_reports([_report("alpha", Verdict.RESTARTED)], path=events, now=NOW)
    # Assert — a fixed problem stops shouting, and says so once.
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED]


def test_a_healthy_agent_without_prior_trouble_records_nothing(events):
    # Arrange — an OK agent that was never down.
    # Act
    record_reports([_report("alpha", Verdict.OK)], path=events, now=NOW)
    # Assert — ~93 healthy agents must not enter the log every five minutes.
    assert not events.exists()


def test_redeath_after_a_recovery_records_again(events):
    # Arrange — down, then fixed (recovery recorded).
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    record_reports([_report("alpha", Verdict.RESTARTED)], path=events, now=NOW)
    # Act — it dies AGAIN; the rail must re-fire, not stay silent.
    record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SUBJECT_DEGRADED, SUBJECT_RECOVERED, SUBJECT_DEGRADED]


def test_a_skipped_agent_gets_no_record(events):
    # Arrange — a deliberately-stopped agent is a CORRECT state, not a
    # problem. Recording it would train the reader to ignore the log.
    # Act
    record_reports([_report("alpha", Verdict.SKIPPED)], path=events, now=NOW)
    # Assert
    assert not events.exists()


def test_an_unknown_agent_gets_no_per_agent_record(events):
    # Arrange — blindness is FLEET-wide (we are in a container, or tmux is
    # wedged). One cause must not mint ~93 records; the pass record carries it.
    # Act
    record_reports([_report("alpha", Verdict.UNKNOWN)], path=events, now=NOW)
    # Assert
    assert not events.exists()


def test_a_cooling_down_agent_gets_no_record(events):
    # Arrange — the debounce is 30min and the timer ticks every 5, so a
    # perfectly HEALTHY restart is cooling down for its next five ticks.
    # Act
    record_reports([_report("alpha", Verdict.COOLING_DOWN)], path=events, now=NOW)
    # Assert
    assert not events.exists()


def test_a_capped_agent_gets_no_record(events):
    # Arrange — CAPPED is sac's own per-pass throttle, not the agent's fault,
    # and the next tick picks it up 5 minutes later.
    # Act
    record_reports([_report("alpha", Verdict.CAPPED)], path=events, now=NOW)
    # Assert
    assert not events.exists()


def test_the_recorded_agent_is_reported_to_the_caller(events):
    # Arrange
    # Act
    outcome = record_reports([_report("alpha", Verdict.FAILED)], path=events, now=NOW)
    # Assert
    assert outcome.degraded == ("alpha",)


def test_one_bad_record_does_not_suppress_the_rest(events, unwritable):
    # Arrange — the log cannot be written at all, so BOTH agents' writes fail.
    # Neither may be silently dropped from the outcome.
    # Act
    outcome = record_reports(
        [_report("alpha", Verdict.FAILED), _report("beta", Verdict.FAILED)],
        path=unwritable,
        now=NOW,
        err_stream=io.StringIO(),
    )
    # Assert
    assert outcome.failed == ("alpha", "beta")


def test_a_recording_failure_is_loud(events, unwritable):
    # Arrange
    stream = io.StringIO()
    # Act
    record_reports(
        [_report("alpha", Verdict.FAILED)],
        path=unwritable,
        now=NOW,
        err_stream=stream,
    )
    # Assert
    assert "FAILED to record" in stream.getvalue()


def test_a_recording_failure_does_not_raise(events, unwritable):
    # Arrange — the pass's job is restarting corpses; recording what it did is
    # secondary and must never be able to take the primary down.
    # Act
    outcome = record_reports(
        [_report("alpha", Verdict.FAILED)],
        path=unwritable,
        now=NOW,
        err_stream=io.StringIO(),
    )
    # Assert
    assert outcome.degraded == ()


# --- the pass record: who watches the watcher ------------------------------


def test_the_pass_record_is_written_on_a_clean_pass(events):
    # Arrange — THE most important tick: "0 restarted, all healthy". A rail
    # that only writes during trouble cannot prove it is alive.
    # Act
    record_pass_completed({"OK": 93}, mode="apply", host="host-a", path=events, now=NOW)
    # Assert
    assert _kinds(events) == [PASS_COMPLETED]


def test_the_pass_record_carries_the_counts(events):
    # Arrange — the counts carry EVERY verdict this pass reached, including
    # the ones that get no per-agent record of their own.
    # Act
    record_pass_completed(
        {"OK": 90, "RESTARTED": 3}, mode="apply", host="host-a", path=events, now=NOW
    )
    # Assert
    assert read_events(events)[0].raw["counts"] == {"OK": 90, "RESTARTED": 3}


def test_the_pass_record_carries_the_mode(events):
    # Arrange — a hand-run dry-run also writes this record, so a reader who
    # ignores ``mode`` can believe the scheduled timer is alive on the
    # strength of somebody having run the command by hand.
    # Act
    record_pass_completed(
        {"OK": 1}, mode="dry-run", host="host-a", path=events, now=NOW
    )
    # Assert
    assert read_events(events)[0].raw["mode"] == "dry-run"


def test_the_pass_record_names_the_host(events):
    # Arrange — one log can hold several hosts' timers; the detail says which
    # machine's reconciler ticked.
    # Act
    record_pass_completed({"OK": 1}, mode="apply", host="host-a", path=events, now=NOW)
    # Assert
    assert "host-a" in read_events(events)[0].detail


def test_the_pass_record_reports_success(events):
    # Arrange
    # Act
    written = record_pass_completed({"OK": 1}, mode="apply", path=events, now=NOW)
    # Assert
    assert written is True


def test_a_pass_record_failure_does_not_raise(events, unwritable):
    # Arrange — a SIDE rail: recording that we are alive must never crash the
    # pass that restarts corpses.
    # Act
    written = record_pass_completed(
        {"OK": 1}, mode="apply", path=unwritable, now=NOW, err_stream=io.StringIO()
    )
    # Assert
    assert written is False


def test_a_pass_record_failure_is_loud(events, unwritable):
    # Arrange — if the beacon dies quietly, nobody learns the watcher is
    # unwatched.
    stream = io.StringIO()
    # Act
    record_pass_completed(
        {"OK": 1}, mode="apply", path=unwritable, now=NOW, err_stream=stream
    )
    # Assert
    assert "FAILED to record" in stream.getvalue()


# --- self-impairment: the reconciler cannot read its OWN memory ------------


def test_an_unreadable_state_is_recorded_as_self_impaired(events):
    # Arrange — with no memory the rate limits are unenforceable, so the pass
    # REFUSES to restart. A refusal nobody hears is a no-op.
    # Act
    record_self_impaired(
        "permission denied", state_file="/denied/hist.json", path=events, now=NOW
    )
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED]


def test_the_self_impaired_record_names_the_state_file(events):
    # Arrange — the operator must be sent to the exact path that is denied.
    # Act
    record_self_impaired(
        "permission denied", state_file="/denied/hist.json", path=events, now=NOW
    )
    # Assert
    assert read_events(events)[0].raw["state_file"] == "/denied/hist.json"


def test_the_self_impaired_record_says_restarts_stopped(events):
    # Arrange — the record must teach the reader what to conclude from it:
    # dead agents are staying dead until this is fixed.
    # Act
    record_self_impaired("permission denied", path=events, now=NOW)
    # Assert
    assert "REFUSED to restart" in read_events(events)[0].detail


def test_a_readable_state_is_recorded_as_self_recovered(events):
    # Arrange — the impairment was recorded once; a fixed problem must record
    # that it is fixed rather than merely going quiet.
    record_self_impaired("permission denied", path=events, now=NOW)
    # Act
    record_self_recovered(state_file="/ok/hist.json", path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED, SELF_RECOVERED]


def test_the_self_impaired_record_belongs_to_no_subject(events):
    # Arrange — the fault is sac's own, not any one agent's. The field is
    # PRESENT-and-null: it does not apply, rather than nobody having recorded it.
    # Act
    record_self_impaired("permission denied", path=events, now=NOW)
    # Assert
    assert read_events(events)[0].raw.get("subject", "MISSING") is None


def test_a_self_impaired_write_failure_does_not_raise(events, unwritable):
    # Arrange
    # Act
    written = record_self_impaired(
        "permission denied", path=unwritable, now=NOW, err_stream=io.StringIO()
    )
    # Assert
    assert written is False


# --- self-state is a TRANSITION, never a per-tick assertion ----------------
#
# The reconciler runs every five minutes forever. Asserting "I am fine" on
# every tick would write hundreds of thousands of records a year AND would
# make ``self-recovered`` mean nothing — a recovery record has to mark an
# actual recovery, or it is not evidence of one. An IMPAIRMENT, by contrast,
# is re-recorded while it stands: an ongoing refusal to act is an ongoing
# fact, and a log that mentions it once and goes quiet cannot be told apart
# from a log written by a pass that has itself died.


def test_a_healthy_pass_records_no_self_recovery(events):
    # Arrange — the reconciler was never impaired, so there is nothing to
    # recover FROM. This is the every-five-minutes case.
    # Act
    record_self_recovered(state_file="/ok/hist.json", path=events, now=NOW)
    # Assert — not an empty log: no log was ever opened.
    assert not events.exists()


def test_a_healthy_pass_reports_nothing_written(events):
    # Arrange — ``False`` here means "nothing changed", never "the write
    # failed"; the caller must not read it as an error.
    # Act
    written = record_self_recovered(state_file="/ok/hist.json", path=events, now=NOW)
    # Assert
    assert written is False


def test_a_standing_impairment_is_recorded_every_pass(events):
    # Arrange — still denied on the next tick, and still refusing to restart.
    record_self_impaired("permission denied", path=events, now=NOW)
    # Act
    record_self_impaired("permission denied", path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED, SELF_IMPAIRED]


def test_a_second_recovery_records_nothing(events):
    # Arrange — impaired, then recovered (the transition is on the record).
    record_self_impaired("permission denied", path=events, now=NOW)
    record_self_recovered(path=events, now=NOW)
    # Act — the next tick is still healthy; it must stay quiet.
    record_self_recovered(path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED, SELF_RECOVERED]


def test_reimpairment_after_a_recovery_records_again(events):
    # Arrange — impaired, then readable again (memory cleared).
    record_self_impaired("permission denied", path=events, now=NOW)
    record_self_recovered(path=events, now=NOW)
    # Act — the mount is revoked AGAIN; the rail must re-fire.
    record_self_impaired("permission denied", path=events, now=NOW)
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED, SELF_RECOVERED, SELF_IMPAIRED]
