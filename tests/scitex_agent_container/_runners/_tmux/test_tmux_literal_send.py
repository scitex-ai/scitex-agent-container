"""The ``-l`` (literal) send contract for the containerized Ink/React TUI.

Root fix for the boot startup-prompt Enter-drop (card
sac-tui-startup-prompt-enter-drop): the containerized ``claude`` TUI silently
DROPS non-literal ``send-keys`` — the source-verified recovery recipe
(``_skills/scitex-agent-container/45_agent-to-agent-recovery-tmux.md``) is
``send-keys -l`` for the TEXT, then a SEPARATE named ``Enter`` (NEVER ``-l``)
to submit. These tests pin that exact argv shape via an injected recording
runner (a real callable — no MagicMock, no monkeypatch: PA-306). STX-TQ002
AAA-markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

from scitex_agent_container._runners._tmux.tmux import TmuxManager


def _zero_sleep(_seconds: float) -> None:  # never wait in tests
    return None


class _RunnerRecorder:
    """Records every argv a ``subprocess.run``-style runner is invoked with."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> None:
        self.argvs.append(list(argv))


class TestSendTextLiteralUsesDashL:
    """``send_text_literal`` pastes with ``-l`` and never submits."""

    def test_text_sent_with_dash_l_flag(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_literal("tui-x", "go work", runner=runner)
        # Assert — one send-keys, ``-l`` immediately before the text.
        assert runner.argvs == [["tmux", "send-keys", "-t", "tui-x", "-l", "go work"]]

    def test_literal_paste_sends_no_enter(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_literal("tui-x", "go work", runner=runner)
        # Assert — a literal paste must NOT submit (no Enter keystroke).
        assert all("Enter" not in argv for argv in runner.argvs)


class TestSendTextAndSubmitLiteralThenEnter:
    """``send_text_and_submit`` = literal text (``-l``) then a SEPARATE Enter."""

    def test_text_leg_uses_dash_l(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_and_submit(
            "tui-x", "mission", sleep_fn=_zero_sleep, runner=runner
        )
        # Assert — first call pastes the text literally.
        assert runner.argvs[0] == ["tmux", "send-keys", "-t", "tui-x", "-l", "mission"]

    def test_enter_leg_is_named_key_without_dash_l(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_and_submit(
            "tui-x", "mission", sleep_fn=_zero_sleep, runner=runner
        )
        # Assert — the submit is a SEPARATE named Enter, never ``-l``.
        assert runner.argvs[-1] == ["tmux", "send-keys", "-t", "tui-x", "Enter"]
