"""Unproven banners emit no confirmed auth failures or restart attempts.

The private execution rail keeps its attempt/outcome contract independently;
these tests invoke it directly, without granting pane-only restart admission.
"""

from pathlib import Path

import pytest

from scitex_agent_container._authevents import (
    AUTH_FAILURE_OBSERVED,
    RESTART_ATTEMPTED,
    RESTART_OUTCOME,
    read_auth_events,
    unresolved_attempts,
)
from scitex_agent_container._authheal._pass import _perform, auth_heal_pass
from scitex_agent_container._reconcile._budget import Budget
from scitex_agent_container._reconcile._rule import Verdict

from ._helpers import NOW, Recorder, stuck


@pytest.mark.parametrize("apply", [False, True])
def test_unproven_banner_leaves_no_auth_failure_or_attempt_checks_recorder_names(tmp_path, history, events, apply):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )
    # Assert
    assert recorder.names == []


@pytest.mark.parametrize("apply", [False, True])
def test_unproven_banner_leaves_no_auth_failure_or_attempt_checks_read_auth_events_event(tmp_path, history, events, apply):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )
    # Assert
    assert read_auth_events(event_log) == []


@pytest.mark.parametrize("apply", [False, True])
def test_unproven_banner_leaves_no_auth_failure_or_attempt_checks_outcome_exit_code(tmp_path, history, events, apply):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )
    # Assert
    assert outcome.exit_code() == 2


@pytest.mark.parametrize("apply", [False, True])
def test_unproven_banner_leaves_no_auth_failure_or_attempt_checks_outcome_reports_reason_auth(tmp_path, history, events, apply):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )
    # Assert
    assert outcome.reports[0].reason == "auth-banner-unproven"


@pytest.mark.parametrize("apply", [False, True])
def test_unproven_banner_leaves_no_auth_failure_or_attempt_checks_history_exists(tmp_path, history, events, apply):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=False,
        restart_fn=recorder,
        capture_fn=lambda: stuck("figrecipe"),
        event_log=event_log,
    )
    # Assert
    assert not history.exists()



@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_r_event_for_r(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert [r.event for r in rows] == [RESTART_ATTEMPTED, RESTART_OUTCOME]


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_rows_attempt_id_rows(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert rows[0].attempt_id == rows[1].attempt_id


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_rows_succeeded_ok(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert rows[1].succeeded is ok


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_any_r_event_auth(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert not any(r.event == AUTH_FAILURE_OBSERVED for r in rows)


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_report_verdict_verdict_restarted(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert report.verdict is (Verdict.RESTARTED if ok else Verdict.FAILED)


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome_checks_bool_unresolved_attempts_rows(tmp_path, ok):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert bool(unresolved_attempts(rows)) is (not ok)



def test_raising_execution_still_has_refutable_failed_outcome_checks_r_event_for_r(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux unavailable"))
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert [r.event for r in rows] == [RESTART_ATTEMPTED, RESTART_OUTCOME]


def test_raising_execution_still_has_refutable_failed_outcome_checks_rows_succeeded(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux unavailable"))
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert rows[-1].succeeded is False


def test_raising_execution_still_has_refutable_failed_outcome_checks_report_verdict_verdict_failed(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux unavailable"))
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert report.verdict is Verdict.FAILED


def test_raising_execution_still_has_refutable_failed_outcome_checks_r_agent_for_r(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux unavailable"))
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=True,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    rows = read_auth_events(event_log)
    # Assert
    assert [r.agent for r in unresolved_attempts(rows)] == ["figrecipe"]



def test_independent_execution_attempts_keep_distinct_ids_checks_len_r_attempt_id(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    for name in ("figrecipe", "crossref-local"):
        _perform(
            name,
            budget=Budget({}),
            apply=True,
            now=NOW,
            restart_fn=Recorder(ok=False),
            budget_detail="",
            event_log=event_log,
        )
    attempts = [r for r in read_auth_events(event_log) if r.event == RESTART_ATTEMPTED]
    # Assert
    assert len({r.attempt_id for r in attempts}) == 2


def test_independent_execution_attempts_keep_distinct_ids_checks_all_r_account_for(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    for name in ("figrecipe", "crossref-local"):
        _perform(
            name,
            budget=Budget({}),
            apply=True,
            now=NOW,
            restart_fn=Recorder(ok=False),
            budget_detail="",
            event_log=event_log,
        )
    attempts = [r for r in read_auth_events(event_log) if r.event == RESTART_ATTEMPTED]
    # Assert
    assert all(r.account is None for r in attempts)



def test_private_execution_check_is_never_recorded_as_an_attempt_checks_recorder_names(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=False,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    # Assert
    assert recorder.names == []


def test_private_execution_check_is_never_recorded_as_an_attempt_checks_read_auth_events_event(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=False,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    # Assert
    assert read_auth_events(event_log) == []


def test_private_execution_check_is_never_recorded_as_an_attempt_checks_report_verdict_verdict_would(tmp_path):
    # Arrange
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    # Act
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=False,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    # Assert
    assert report.verdict is Verdict.WOULD_RESTART



def test_real_healthy_idle_specimen_is_never_restarted_checks_recorder_names(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert recorder.names == []


def test_real_healthy_idle_specimen_is_never_restarted_checks_read_auth_events_event(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert read_auth_events(event_log) == []


def test_real_healthy_idle_specimen_is_never_restarted_checks_history_read_bytes_before(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert history.read_bytes() == before


def test_real_healthy_idle_specimen_is_never_restarted_checks_outcome_reports_reason_historical(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert outcome.reports[0].reason == "historical-auth-banner"


def test_real_healthy_idle_specimen_is_never_restarted_checks_report_only_outcome_reports(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert "report-only" in outcome.reports[0].detail


def test_real_healthy_idle_specimen_is_never_restarted_checks_outcome_exit_code(tmp_path, history, events):
    # Arrange
    specimen = (
        Path(__file__).parents[1]
        / "fixtures"
        / "pane_states"
        / "specimen_grant_20260718_alive_false_positive.log"
    ).read_text()
    start = specimen.index("\n", specimen.index("--- pane capture")) + 1
    pane = specimen[start : specimen.index("--- state.db row ---")]
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    before = b'{"restarts":{},"schema_version":1}\n'
    history.write_bytes(before)
    # Act
    outcome = auth_heal_pass(
        apply=True,
        now=NOW,
        history_file=history,
        events_path=events,
        alarm=True,
        restart_fn=recorder,
        capture_fn=lambda: {"grant": (pane, pane)},
        event_log=event_log,
    )
    # Assert
    assert outcome.exit_code() == 2

