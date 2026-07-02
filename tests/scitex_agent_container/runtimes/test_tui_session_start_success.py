"""Unit tests for the BUG 3 start success/failure decision + Esc-cancel detector.

Card sac-boot-automation-devchannels-modal-continue-compose-buffer:

  * BUG 3 (false success — constitution §2 "no surprises / fail loud"):
    ``start_succeeded`` must report FAILURE when the session died or never
    reached ready, so ``agent_start`` stops printing "restarted" over a corpse.
  * BUG 1 detector: ``has_esc_cancel_modal`` must flag any modal whose Esc
    cancels the launch (dev-channels; any "Esc to cancel" footer).

Pure functions — no fakes needed. AAA markers, one assert each.
"""

from __future__ import annotations

from scitex_agent_container.runtimes.prompts import has_esc_cancel_modal
from scitex_agent_container.runtimes.tui_session import start_succeeded

# ---------------------------------------------------------------------------
# start_succeeded — BUG 3 (no false success)
# ---------------------------------------------------------------------------


def test_start_failure_when_session_dead() -> None:
    # Arrange
    args = dict(session_alive=False, reached_ready=True, is_running=True)
    # Act
    ok = start_succeeded(**args)
    # Assert — a dead session is never a success, whatever the drain thought.
    assert ok is False


def test_start_success_when_alive_and_reached_ready() -> None:
    # Arrange
    args = dict(session_alive=True, reached_ready=True, is_running=False)
    # Act
    ok = start_succeeded(**args)
    # Assert
    assert ok is True


def test_start_failure_when_alive_but_never_reached_ready() -> None:
    # Arrange — drain RAN and did not reach ready → loud failure.
    args = dict(session_alive=True, reached_ready=False, is_running=True)
    # Act
    ok = start_succeeded(**args)
    # Assert
    assert ok is False


def test_start_defers_to_liveness_when_drain_disabled() -> None:
    # Arrange — drain disabled (None) → defer to is_running.
    args = dict(session_alive=True, reached_ready=None, is_running=True)
    # Act
    ok = start_succeeded(**args)
    # Assert
    assert ok is True


def test_start_failure_when_drain_disabled_and_not_running() -> None:
    # Arrange
    args = dict(session_alive=True, reached_ready=None, is_running=False)
    # Act
    ok = start_succeeded(**args)
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# has_esc_cancel_modal — BUG 1 detector
# ---------------------------------------------------------------------------


def test_esc_cancel_detected_for_dev_channels_modal() -> None:
    # Arrange
    content = "1. I am using this for local development\nEnter to confirm"
    # Act
    result = has_esc_cancel_modal(content)
    # Assert
    assert result is True


def test_esc_cancel_detected_for_esc_to_cancel_footer() -> None:
    # Arrange — any modal rendering the "Esc to cancel" footer.
    content = "Some confirmation dialog\nEnter to confirm · Esc to cancel"
    # Act
    result = has_esc_cancel_modal(content)
    # Assert
    assert result is True


def test_esc_cancel_false_for_plain_input_line() -> None:
    # Arrange — a normal ready pane (safe to Escape a stale compose buffer).
    content = "❯ stale pending text\nbypass permissions"
    # Act
    result = has_esc_cancel_modal(content)
    # Assert
    assert result is False
