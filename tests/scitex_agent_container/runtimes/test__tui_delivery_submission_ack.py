"""A visible-channel ACK requires Codex to admit the pasted turn.

The regression was a legitimate Cards CI notification left in Codex's live
composer after Enter was dropped.  ``send_turn_to_pane`` nevertheless returned
True, so Cards confirmed it and the parked composer then blocked CCT.  These
tests use Codex-shaped pane frames and the real submission verifier.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._tui_delivery import send_turn_to_pane

_PAYLOAD = "CI feedback [channel-submit:7f6c2d]: tests failed"
_PENDING = (
    "› CI feedback [channel-submit:7f6c2d]: tests failed\n"
    "  gpt-5.6-sol high · /home/ywatanabe/proj/scitex-agent-container\n"
)
_SUBMITTED = (
    "› CI feedback [channel-submit:7f6c2d]: tests failed\n"
    "• Working (0s • esc to interrupt)\n"
    "› Ask Codex to do anything\n"
    "  gpt-5.6-sol high · /home/ywatanabe/proj/scitex-agent-container\n"
)


class _CodexMux:
    submitted = False
    drop_enter = False
    pasted: list[str] = []

    @classmethod
    def reset(cls, *, drop_enter: bool) -> None:
        cls.submitted = False
        cls.drop_enter = drop_enter
        cls.pasted = []

    @staticmethod
    def exists(_name: str) -> bool:
        return True

    @classmethod
    def capture_content(cls, _name: str) -> str:
        if not cls.pasted:
            return "› Ask Codex to do anything\n  gpt-5.6-sol high\n"
        return _SUBMITTED if cls.submitted else _PENDING

    @classmethod
    def send_text_literal(cls, _name: str, text: str) -> None:
        cls.pasted.append(text)

    @classmethod
    def send_keys(cls, _name: str, key: str) -> None:
        if key == "Enter" and not cls.drop_enter:
            cls.submitted = True


def _deliver() -> bool:
    return send_turn_to_pane(
        _CodexMux,
        "tui-agent",
        _PAYLOAD,
        wait_ready=False,
        require_accepting=False,
        submission_poll_s=0.0,
        submission_appear_timeout_s=0.01,
        submission_idle_wait_s=0.01,
        submission_proof_stable_s=0.0,
        submission_max_resends=2,
    )


def test_codex_turn_is_acknowledged_after_observable_submission() -> None:
    # Arrange
    _CodexMux.reset(drop_enter=False)

    # Act
    delivered = _deliver()

    # Assert
    assert delivered is True


def test_codex_turn_is_not_acknowledged_when_enter_is_dropped() -> None:
    # Arrange
    _CodexMux.reset(drop_enter=True)

    # Act
    delivered = _deliver()

    # Assert
    assert delivered is False


def test_legitimate_ci_feedback_is_not_suppressed() -> None:
    # Arrange
    _CodexMux.reset(drop_enter=False)

    # Act
    _deliver()

    # Assert
    assert _CodexMux.pasted == [_PAYLOAD]
