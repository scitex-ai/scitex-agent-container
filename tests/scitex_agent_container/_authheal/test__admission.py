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
def test_banner_position_or_long_silence_never_grants_admission(pane, observed_s):
    admission = banner_restart_admission(
        "hermes-native", (pane, pane), observed_s=observed_s
    )
    assert admission.allowed is False
    assert admission.report.verdict is Verdict.UNOBSERVED
    assert "Native Hermes requires session/turn telemetry" in admission.report.detail


@pytest.mark.parametrize("apply", [False, True])
def test_live_native_hermes_banner_never_invokes_successor_or_restart(
    history, events, tmp_path, apply
):
    recorder = Recorder()
    event_log = tmp_path / "auth-events.jsonl"
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
    assert recorder.names == []
    assert not history.exists()
    assert not event_log.exists()
    assert outcome.heartbeat_ok is True
    assert outcome.exit_code() == 2
    assert outcome.reports[0].reason == "auth-banner-unproven"
