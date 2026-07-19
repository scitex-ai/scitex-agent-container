"""The TUI strategy's own units — especially the guard that got this wrong once.

``_submission_signal`` is tested directly and exhaustively because its BRANCH
ORDER is the whole correctness argument: the wholly-blind check must run BEFORE
``submitted`` is consulted, since the submit verifier returns ``True`` from its
"nothing pending to force" phase even when every capture it read was the empty
string substituted for an unreadable pane.
"""

from __future__ import annotations

from scitex_agent_container._delivery._state import DeliveryState
from scitex_agent_container._delivery._tui_strategy import (
    CaptureTap,
    _submission_signal,
    observe_pane_before,
)

_IDLE = "  earlier turn\n────────\n❯\n────────\n  ctx:1%\n"
_BUSY = "  earlier turn\n────────\n❯\n────────\n  esc to interrupt\n"
_BANNER = "● Login expired · Please run /login\n────────\n❯\n────────\n  ctx:1%\n"


class ScriptedCapture:
    """A real ``capture_fn(session) -> str | None`` returning a fixed answer."""

    def __init__(self, pane):
        self._pane = pane

    def __call__(self, session):
        return self._pane


def _signal(**kwargs):
    base = dict(
        submitted=True,
        readable_during=5,
        blind_during=0,
        max_resends=8,
        session="tui-peer",
    )
    base.update(kwargs)
    return _submission_signal(**base)


# --- _submission_signal: the tri-state guard --------------------------------


def test_observed_clear_reports_submitted_true():
    # Arrange
    kwargs = dict(submitted=True, readable_during=5, blind_during=0)
    # Act
    value, _ = _signal(**kwargs)
    # Assert
    assert value is True


def test_wholly_blind_window_refuses_a_verdict():
    # Arrange
    kwargs = dict(submitted=True, readable_during=0, blind_during=40)
    # Act
    value, _ = _signal(**kwargs)
    # Assert
    assert value is None


def test_wholly_blind_window_names_it_vacuous():
    # Arrange
    kwargs = dict(submitted=True, readable_during=0, blind_during=40)
    # Act
    _, reason = _signal(**kwargs)
    # Assert
    assert "vacuous" in reason


def test_partly_blind_failure_is_not_refutation():
    # Arrange
    kwargs = dict(submitted=False, readable_during=3, blind_during=2)
    # Act
    value, _ = _signal(**kwargs)
    # Assert
    assert value is None


def test_partly_blind_success_is_still_trusted():
    # Arrange
    kwargs = dict(submitted=True, readable_during=3, blind_during=2)
    # Act
    value, _ = _signal(**kwargs)
    # Assert
    assert value is True


def test_fully_observed_failure_refutes_cleanly():
    # Arrange
    kwargs = dict(submitted=False, readable_during=9, blind_during=0)
    # Act
    value, _ = _signal(**kwargs)
    # Assert
    assert value is False


def test_observed_failure_forbids_a_resend():
    # Arrange
    kwargs = dict(submitted=False, readable_during=9, blind_during=0)
    # Act
    _, reason = _signal(**kwargs)
    # Assert
    assert "Do NOT resend" in reason


def test_observed_failure_offers_the_attach_hint():
    # Arrange
    kwargs = dict(submitted=False, readable_during=9, blind_during=0)
    # Act
    _, reason = _signal(**kwargs)
    # Assert
    assert "tmux attach -t tui-peer" in reason


# --- CaptureTap: the tri-state that the verifier's type cannot hold ---------


def test_tap_counts_an_unreadable_capture():
    # Arrange
    tap = CaptureTap(ScriptedCapture(None))
    # Act
    tap("tui-peer")
    # Assert
    assert tap.unreadable == 1


def test_tap_substitutes_empty_for_unreadable():
    # Arrange
    tap = CaptureTap(ScriptedCapture(None))
    # Act
    seen = tap("tui-peer")
    # Assert
    assert seen == ""


def test_tap_read_preserves_the_none():
    # Arrange
    tap = CaptureTap(ScriptedCapture(None))
    # Act
    seen = tap.read("tui-peer")
    # Assert
    assert seen is None


def test_tap_remembers_the_last_readable():
    # Arrange
    tap = CaptureTap(ScriptedCapture(_IDLE))
    # Act
    tap("tui-peer")
    # Assert
    assert tap.last_readable == _IDLE


# --- observe_pane_before: evidence, never a veto ----------------------------


def test_busy_pane_is_observed_busy():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, _BUSY)
    # Assert
    assert observed.is_target_busy_before is True


def test_idle_pane_is_observed_not_busy():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, _IDLE)
    # Assert
    assert observed.is_target_busy_before is False


def test_banner_pane_is_observed_banner():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, _BANNER)
    # Assert
    assert observed.is_login_banner_before is True


def test_banner_reason_flags_it_uncorroborated():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, _BANNER)
    # Assert
    assert "UNCORROBORATED" in observed.reason_for("is_login_banner_before")


def test_unreadable_pane_leaves_busy_unknown():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, None)
    # Assert
    assert observed.is_target_busy_before is None


def test_unreadable_pane_marks_pane_unreadable():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, None)
    # Assert
    assert observed.is_pane_readable is False


def test_readable_pane_keeps_the_raw_capture():
    # Arrange
    state = DeliveryState(agent="peer")
    # Act
    observed = observe_pane_before(state, _IDLE)
    # Assert
    assert observed.raw["pane_before"] == _IDLE
