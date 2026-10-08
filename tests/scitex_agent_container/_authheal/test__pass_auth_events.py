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
def test_unproven_banner_leaves_no_auth_failure_or_attempt(
    tmp_path, history, events, apply
):
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
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
    assert recorder.names == []
    assert read_auth_events(event_log) == []
    assert outcome.exit_code() == 2
    assert outcome.reports[0].reason == "auth-banner-unproven"
    assert not history.exists()


@pytest.mark.parametrize("ok", [False, True])
def test_execution_rail_records_distinct_attempt_and_outcome(tmp_path, ok):
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(ok=ok)
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
    assert [r.event for r in rows] == [RESTART_ATTEMPTED, RESTART_OUTCOME]
    assert rows[0].attempt_id == rows[1].attempt_id
    assert rows[1].succeeded is ok
    assert not any(r.event == AUTH_FAILURE_OBSERVED for r in rows)
    assert report.verdict is (Verdict.RESTARTED if ok else Verdict.FAILED)
    assert bool(unresolved_attempts(rows)) is (not ok)


def test_raising_execution_still_has_refutable_failed_outcome(tmp_path):
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder(boom=RuntimeError("tmux unavailable"))
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
    assert [r.event for r in rows] == [RESTART_ATTEMPTED, RESTART_OUTCOME]
    assert rows[-1].succeeded is False
    assert report.verdict is Verdict.FAILED
    assert [r.agent for r in unresolved_attempts(rows)] == ["figrecipe"]


def test_independent_execution_attempts_keep_distinct_ids(tmp_path):
    event_log = tmp_path / "auth-events.jsonl"
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
    assert len({r.attempt_id for r in attempts}) == 2
    assert all(r.account is None for r in attempts)


def test_private_execution_check_is_never_recorded_as_an_attempt(tmp_path):
    event_log = tmp_path / "auth-events.jsonl"
    recorder = Recorder()
    report = _perform(
        "figrecipe",
        budget=Budget({}),
        apply=False,
        now=NOW,
        restart_fn=recorder,
        budget_detail="",
        event_log=event_log,
    )
    assert recorder.names == []
    assert read_auth_events(event_log) == []
    assert report.verdict is Verdict.WOULD_RESTART


def test_real_healthy_idle_specimen_is_never_restarted(tmp_path, history, events):
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
    assert recorder.names == []
    assert read_auth_events(event_log) == []
    assert history.read_bytes() == before
    assert outcome.reports[0].reason == "historical-auth-banner"
    assert "report-only" in outcome.reports[0].detail
    assert outcome.exit_code() == 2
