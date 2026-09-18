"""Atomic literal-paste and separate-submit contract for interactive TUIs.

Root fix for the boot startup-prompt Enter-drop (card
sac-tui-startup-prompt-enter-drop): the containerized ``claude`` TUI silently
DROPS streamed key events. Text now enters through one bracketed tmux buffer
paste, followed by a SEPARATE named ``Enter`` to submit. These tests pin that
argv shape via an injected recording runner (a real callable, no mocks).
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


class TestSendTextLiteralUsesBracketedPaste:
    """``send_text_literal`` uses one tmux buffer paste and never submits."""

    def test_text_sent_with_bracketed_paste(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_literal("tui-x", "go work", runner=runner)
        # Assert
        assert runner.argvs[1][0:4] + runner.argvs[1][-2:] == [
            "tmux",
            "paste-buffer",
            "-p",
            "-r",
            "-t",
            "=tui-x:",
        ]

    def test_literal_paste_sends_no_enter(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_literal("tui-x", "go work", runner=runner)
        # Assert — a literal paste must NOT submit (no Enter keystroke).
        assert all("Enter" not in argv for argv in runner.argvs)


class TestSendTextAndSubmitLiteralThenEnter:
    """``send_text_and_submit`` = bracketed paste then a SEPARATE Enter."""

    def test_text_leg_uses_bracketed_paste(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_and_submit(
            "tui-x", "mission", sleep_fn=_zero_sleep, runner=runner
        )
        # Assert
        assert runner.argvs[1][0:4] + runner.argvs[1][-2:] == [
            "tmux",
            "paste-buffer",
            "-p",
            "-r",
            "-t",
            "=tui-x:",
        ]

    def test_enter_leg_is_named_key_without_dash_l(self) -> None:
        # Arrange
        runner = _RunnerRecorder()
        # Act
        TmuxManager.send_text_and_submit(
            "tui-x", "mission", sleep_fn=_zero_sleep, runner=runner
        )
        # Assert — the submit is a SEPARATE named Enter, never ``-l``
        # (exact =name: target, same as the text leg).
        assert runner.argvs[-1] == ["tmux", "send-keys", "-t", "=tui-x:", "Enter"]
