"""The four indistinguishable modes, each driven end-to-end and each SEPARATED.

Every test here runs the REAL production path — the real route resolver, the real
busy classifier, the real auth-banner matcher, the real
``verify_submit_by_advancement`` — against a hand-rolled compose box that
reproduces the TUI behaviours that caused the incident. No mocks, no monkeypatch.

The modes, and the exit code each one now produces:

1. TARGET DEAD              → 3 (was: silent success)
2. TARGET UNREADABLE/BLIND  → 2 (was: silent success)
3. TEXT SITS UNSUBMITTED    → 4 (was: silent success)
4. VERIFICATION LIES        → 0, correctly, where a prose grep said "not
                                delivered" about a message that had arrived
"""

from __future__ import annotations

from scitex_agent_container._delivery import (
    EXIT_DELIVERED,
    EXIT_NO_ROUTE,
    EXIT_UNKNOWN,
    EXIT_UNSUBMITTED,
    assess_delivery,
    deliver,
)

from ._helpers import ComposerPane, TickClock


class SessionLister:
    """A real ``list_sessions_fn() -> list[str] | None``."""

    def __init__(self, sessions):
        self._sessions = sessions

    def __call__(self):
        return self._sessions


class SessionIdReader:
    """A real ``session_id_fn(agent) -> str | None``."""

    def __init__(self, session_id):
        self._session_id = session_id

    def __call__(self, agent):
        return self._session_id


class SdkSender:
    """A real ``sdk_send_fn(agent, payload) -> (bool | None, str)``."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def __call__(self, agent, payload):
        self.calls.append((agent, payload))
        return self._outcome


def _deliver(pane, *, sessions=("tui-peer",), max_resends=3, **overrides):
    """Run the production ``deliver`` against ``pane``, on an injected clock."""
    clock = TickClock()
    kwargs = dict(
        strategy="tui",
        list_sessions_fn=SessionLister(list(sessions)),
        capture_fn=pane.capture,
        paste_fn=pane.paste,
        send_keys_fn=pane.send_key,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
        clock_fn=lambda: 1_800_000_000.0,
        poll_s=0.1,
        arrival_timeout_s=2.0,
        idle_wait_s=2.0,
        max_resends=max_resends,
    )
    kwargs.update(overrides)
    return deliver("peer", "please rebase onto develop", **kwargs)


# --- the happy path ---------------------------------------------------------


def test_idle_target_reports_delivered_verdict():
    # Arrange
    pane = ComposerPane()
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.verdict is True


def test_idle_target_exits_delivered_code():
    # Arrange
    pane = ComposerPane()
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.exit_code() == EXIT_DELIVERED


def test_delivered_payload_reaches_the_transcript():
    # Arrange
    pane = ComposerPane()
    # Act
    state = _deliver(pane)
    # Assert
    assert state.token in pane.submitted[0]


def test_successful_send_needs_one_enter():
    # Arrange
    pane = ComposerPane()
    # Act
    _deliver(pane)
    # Assert
    assert pane.enters == 1


# --- mode 1: TARGET DEAD ----------------------------------------------------


def test_missing_session_exits_no_route():
    # Arrange
    pane = ComposerPane()
    # Act
    verdict = assess_delivery(_deliver(pane, sessions=("tui-someone-else",)))
    # Assert
    assert verdict.exit_code() == EXIT_NO_ROUTE


def test_missing_session_never_pastes_anything():
    # Arrange
    pane = ComposerPane()
    # Act
    _deliver(pane, sessions=("tui-someone-else",))
    # Assert
    assert pane.buffer == ""


def test_missing_session_states_a_known_negative():
    # Arrange
    pane = ComposerPane()
    # Act
    state = _deliver(pane, sessions=("tui-someone-else",))
    # Assert
    assert state.is_payload_delivered is False


# --- mode 1b: BLIND, which must NOT read as dead ----------------------------


def test_empty_enumeration_exits_unknown_not_dead():
    # Arrange
    pane = ComposerPane()
    # Act
    verdict = assess_delivery(_deliver(pane, sessions=()))
    # Assert
    assert verdict.exit_code() == EXIT_UNKNOWN


# --- mode 2: UNREADABLE PANE ------------------------------------------------


def test_unreadable_pane_exits_could_not_determine():
    # Arrange
    pane = ComposerPane(readable=False)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.exit_code() == EXIT_UNKNOWN


def test_unreadable_pane_reports_arrival_unknown():
    # Arrange
    pane = ComposerPane(readable=False)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_payload_delivered is None


def test_unreadable_pane_never_claims_submission():
    # Arrange
    pane = ComposerPane(readable=False)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_payload_submitted is None


def test_blind_submit_is_named_vacuous():
    # Arrange
    pane = ComposerPane(readable=False)
    # Act
    state = _deliver(pane)
    # Assert
    assert "vacuous" in state.reason_for("is_payload_submitted")


def test_unreadable_pane_is_reported_unreadable():
    # Arrange
    pane = ComposerPane(readable=False)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_pane_readable is False


# --- mode 3: TEXT SITS UNSUBMITTED IN THE COMPOSER --------------------------


def test_dropped_enters_exit_unsubmitted_code():
    # Arrange
    pane = ComposerPane(drops_enter=99)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.exit_code() == EXIT_UNSUBMITTED


def test_unsubmitted_still_reports_arrival_true():
    # Arrange
    pane = ComposerPane(drops_enter=99)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_payload_delivered is True


def test_unsubmitted_advises_against_resending():
    # Arrange
    pane = ComposerPane(drops_enter=99)
    # Act
    state = _deliver(pane)
    # Assert
    assert "Do NOT resend" in state.reason_for("is_payload_submitted")


def test_submit_retry_recovers_a_dropped_enter():
    # Arrange
    pane = ComposerPane(drops_enter=1)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_payload_submitted is True


def test_recovering_retry_sends_a_second_enter():
    # Arrange
    pane = ComposerPane(drops_enter=1)
    # Act
    _deliver(pane)
    # Assert
    assert pane.enters == 2


def test_submit_budget_is_bounded_by_resends():
    # Arrange
    pane = ComposerPane(drops_enter=99)
    # Act
    _deliver(pane, max_resends=2)
    # Assert
    assert pane.enters == 2


# --- the busy window, which is what EATS the Enter --------------------------


def test_busy_target_gets_no_enter_while_busy():
    # Arrange
    pane = ComposerPane(busy_captures=4)
    # Act
    _deliver(pane)
    # Assert
    assert pane.enters_while_busy == 0


def test_busy_target_still_delivers_successfully():
    # Arrange
    pane = ComposerPane(busy_captures=4)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.verdict is True


def test_busy_before_send_is_recorded():
    # Arrange
    pane = ComposerPane(busy_captures=4)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_target_busy_before is True


def test_busy_evidence_does_not_refute_delivery():
    # Arrange
    pane = ComposerPane(busy_captures=4)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.exit_code() == EXIT_DELIVERED


# --- mode 4: THE VERIFICATION ITSELF LIES -----------------------------------


def test_wrapped_render_still_confirms_arrival():
    # Arrange
    pane = ComposerPane(wrap_at=8)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_payload_delivered is True


def test_wrapped_render_exits_delivered_code():
    # Arrange
    pane = ComposerPane(wrap_at=8)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.exit_code() == EXIT_DELIVERED


def test_wrapped_token_defeats_naive_substring():
    # Arrange
    pane = ComposerPane(wrap_at=8)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.token not in state.raw["pane_after_paste"]


# --- the auth banner, surfaced but never allowed to refute ------------------


def test_login_banner_before_send_is_surfaced():
    # Arrange
    pane = ComposerPane(banner=True)
    # Act
    state = _deliver(pane)
    # Assert
    assert state.is_login_banner_before is True


def test_login_banner_does_not_refute_delivery():
    # Arrange
    pane = ComposerPane(banner=True)
    # Act
    verdict = assess_delivery(_deliver(pane))
    # Assert
    assert verdict.verdict is True


# --- ONE verb, TWO strategies -----------------------------------------------


def test_recorded_session_uses_the_sdk_path():
    # Arrange
    sender = SdkSender((True, "send_to_agent completed the turn"))
    # Act
    _deliver(
        ComposerPane(),
        strategy="auto",
        session_id_fn=SessionIdReader("sid-abc"),
        sdk_send_fn=sender,
    )
    # Assert
    assert len(sender.calls) == 1


def test_sdk_path_never_touches_the_pane():
    # Arrange
    pane = ComposerPane()
    # Act
    _deliver(
        pane,
        strategy="auto",
        session_id_fn=SessionIdReader("sid-abc"),
        sdk_send_fn=SdkSender((True, "ok")),
    )
    # Assert
    assert pane.captures == 0


def test_sdk_success_verdicts_delivered():
    # Arrange
    sender = SdkSender((True, "send_to_agent completed the turn"))
    # Act
    verdict = assess_delivery(
        _deliver(
            ComposerPane(),
            strategy="auto",
            session_id_fn=SessionIdReader("sid-abc"),
            sdk_send_fn=sender,
        )
    )
    # Assert
    assert verdict.verdict is True


def test_sdk_timeout_verdicts_could_not_determine():
    # Arrange
    sender = SdkSender((None, "timed out waiting for the reply"))
    # Act
    verdict = assess_delivery(
        _deliver(
            ComposerPane(),
            strategy="auto",
            session_id_fn=SessionIdReader("sid-abc"),
            sdk_send_fn=sender,
        )
    )
    # Assert
    assert verdict.exit_code() == EXIT_UNKNOWN


def test_sdk_path_records_its_strategy():
    # Arrange
    sender = SdkSender((True, "ok"))
    # Act
    state = _deliver(
        ComposerPane(),
        strategy="auto",
        session_id_fn=SessionIdReader("sid-abc"),
        sdk_send_fn=sender,
    )
    # Assert
    assert state.strategy == "sdk"


def test_tui_path_records_its_strategy():
    # Arrange
    pane = ComposerPane()
    # Act
    state = _deliver(pane)
    # Assert
    assert state.strategy == "tui"


# --- the payload carries a greppable token ----------------------------------


def test_pasted_payload_carries_the_token():
    # Arrange
    pane = ComposerPane()
    # Act
    state = _deliver(pane)
    # Assert
    assert f"[sac-deliver:{state.token}]" in pane.submitted[0]


def test_elapsed_time_is_always_stamped():
    # Arrange
    pane = ComposerPane()
    # Act
    state = _deliver(pane)
    # Assert
    assert state.elapsed is not None
