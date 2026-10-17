"""No pane-only true/false positive becomes destructive restart admission."""

import pytest

from scitex_agent_container._authheal._admission import banner_restart_admission
from scitex_agent_container._authheal._pass import auth_heal_pass
from scitex_agent_container._reconcile._rule import Verdict

from ._helpers import NOW, STUCK, Recorder


@pytest.mark.parametrize("observed_s", [4.0, 90.0, 9000.0])
@pytest.mark.parametrize(
    "pane",
    [
        STUCK,
        "Start or continue.\n" + STUCK,
        "Login expired\nStart or continue.\n────────\n❯\n",
    ],
)
def test_banner_position_or_long_silence_never_grants_admission_checks_admission_allowed(pane, observed_s):
    # Arrange
    # Act
    admission = banner_restart_admission(
        "hermes-native", (pane, pane), observed_s=observed_s
    )
    # Assert
    assert admission.allowed is False


@pytest.mark.parametrize("observed_s", [4.0, 90.0, 9000.0])
@pytest.mark.parametrize(
    "pane",
    [
        STUCK,
        "Start or continue.\n" + STUCK,
        "Login expired\nStart or continue.\n────────\n❯\n",
    ],
)
def test_banner_position_or_long_silence_never_grants_admission_checks_admission_report_verdict_verdict(pane, observed_s):
    # Arrange
    # Act
    admission = banner_restart_admission(
        "hermes-native", (pane, pane), observed_s=observed_s
    )
    # Assert
    assert admission.report.verdict is Verdict.UNOBSERVED


@pytest.mark.parametrize("observed_s", [4.0, 90.0, 9000.0])
@pytest.mark.parametrize(
    "pane",
    [
        STUCK,
        "Start or continue.\n" + STUCK,
        "Login expired\nStart or continue.\n────────\n❯\n",
    ],
)
def test_banner_position_or_long_silence_never_grants_admission_checks_native_hermes_requires_session(pane, observed_s):
    # Arrange
    # Act
    admission = banner_restart_admission(
        "hermes-native", (pane, pane), observed_s=observed_s
    )
    # Assert
    assert "Native Hermes requires session/turn telemetry" in admission.report.detail



@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_recorder_names(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert recorder.names == []


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_history_exists(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert not history.exists()


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_event_log_exists(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert not event_log.exists()


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_outcome_heartbeat_ok(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert outcome.heartbeat_ok is True


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_outcome_exit_code(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert outcome.exit_code() == 2


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart_checks_outcome_reports_reason_auth(history, events, tmp_path, apply):
    # Arrange
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
    # Act
    outcome = auth_heal_pass(
        apply=apply,
        now=NOW,
        capture_fn=lambda: {"hermes-native": (STUCK, STUCK)},
        restart_fn=recorder,
        history_file=history,
        events_path=events,
        event_log=event_log,
        interval=90.0,
    )
    # Assert
    assert outcome.reports[0].reason == "auth-banner-unproven"

