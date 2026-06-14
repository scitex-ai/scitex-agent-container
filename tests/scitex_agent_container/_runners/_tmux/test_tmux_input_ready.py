"""Tests for the Ink-drop structural fix primitives on TmuxManager.

Lead a2a ``910ff436642948eb85f8b3100204ed9b`` (2026-06-14): the
interactive claude TUI silently drops keystrokes when its Ink
renderer is mid-frame. The fix is observation-based — wait for the
input-ready marker before sending, then verify the send echoed back
before committing Enter, retrying on silent drops. Two primitives:

  * ``TmuxManager.wait_for_input_ready`` — polls capture-pane until
    a marker appears; raises :class:`TuiInputNotReadyError` on
    timeout (never silent).
  * ``TmuxManager.send_text_and_submit_verified`` — sends text, polls
    for echo, retries on Ink drop, then commits Enter. Raises
    :class:`TuiKeystrokeDropError` after all retries exhausted.

Both expose injection seams (``capture_fn``, ``send_text_fn``,
``send_enter_fn``, ``sleep_fn``) so the suite exercises every branch
without subprocess.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._tmux.tmux import (
    TmuxManager,
    TuiInputNotReadyError,
    TuiKeystrokeDropError,
)

# ---------------------------------------------------------------------------
# Injection-seam helpers — real classes/functions, not MagicMocks.
# ---------------------------------------------------------------------------


class _ScriptedCapture:
    """Returns successive lines on each call; loops on the last entry.

    A real class instead of a generator so the suite can probe how
    many captures were issued (probes ``calls`` after the fact).
    """

    def __init__(self, frames: list[str]) -> None:
        if not frames:
            raise ValueError("at least one frame required")
        self._frames = frames
        self._idx = 0
        self.calls = 0

    def __call__(self, _session: str) -> str:
        self.calls += 1
        frame = self._frames[self._idx]
        if self._idx < len(self._frames) - 1:
            self._idx += 1
        return frame


class _Recorder:
    """Records every (session, payload) it is invoked with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session: str, payload: str = "") -> None:
        self.calls.append((session, payload))


def _zero_sleep(_seconds: float) -> None:  # never wait in tests
    return None


# ---------------------------------------------------------------------------
# wait_for_input_ready
# ---------------------------------------------------------------------------


class TestWaitForInputReadyImmediateMarker:
    """Marker visible on the first capture → returns True immediately."""

    def test_returns_true_when_marker_present_first_frame(self) -> None:
        # Arrange
        capture = _ScriptedCapture(["banner\n? for shortcuts\n"])
        # Act
        result = TmuxManager.wait_for_input_ready(
            "s",
            capture_fn=capture,
            sleep_fn=_zero_sleep,
            timeout_s=5.0,
        )
        # Assert
        assert result is True

    def test_one_capture_when_marker_present_first_frame(self) -> None:
        # Arrange
        capture = _ScriptedCapture(["banner\n? for shortcuts\n"])
        # Act
        TmuxManager.wait_for_input_ready(
            "s",
            capture_fn=capture,
            sleep_fn=_zero_sleep,
            timeout_s=5.0,
        )
        # Assert
        assert capture.calls == 1


class TestWaitForInputReadyAfterRender:
    """Marker arrives on a later frame → polls and returns True."""

    def test_polls_until_marker_appears(self) -> None:
        # Arrange — first 2 frames lack the marker; third has it.
        capture = _ScriptedCapture(
            [
                "Loading...\n",
                "Welcome back\n",
                "Welcome back\n? for shortcuts\n",
            ]
        )
        # Act
        result = TmuxManager.wait_for_input_ready(
            "s",
            capture_fn=capture,
            sleep_fn=_zero_sleep,
            poll_s=0.01,
            timeout_s=5.0,
        )
        # Assert
        assert result is True


class TestWaitForInputReadyTimeout:
    """Marker never appears → raises :class:`TuiInputNotReadyError`."""

    def test_timeout_raises_named_exception(self) -> None:
        # Arrange — capture never yields the marker; tiny timeout.
        capture = _ScriptedCapture(["no marker here\n"])
        # Act / Assert
        with pytest.raises(TuiInputNotReadyError, match="input-ready marker"):
            TmuxManager.wait_for_input_ready(
                "s",
                capture_fn=capture,
                sleep_fn=_zero_sleep,
                poll_s=0.0,
                timeout_s=0.01,
            )

    def test_timeout_message_includes_marker_repr(self) -> None:
        # Arrange
        capture = _ScriptedCapture(["nope\n"])
        # Act
        with pytest.raises(TuiInputNotReadyError) as exc_info:
            TmuxManager.wait_for_input_ready(
                "s",
                marker="CUSTOM-READY",
                capture_fn=capture,
                sleep_fn=_zero_sleep,
                poll_s=0.0,
                timeout_s=0.01,
            )
        # Assert
        assert "'CUSTOM-READY'" in str(exc_info.value)


class TestWaitForInputReadyCustomMarker:
    """Caller can override the marker (non-claude TUIs)."""

    def test_custom_marker_recognised(self) -> None:
        # Arrange
        capture = _ScriptedCapture(["bash$\n"])
        # Act
        result = TmuxManager.wait_for_input_ready(
            "s",
            marker="bash$",
            capture_fn=capture,
            sleep_fn=_zero_sleep,
            timeout_s=5.0,
        )
        # Assert
        assert result is True


# ---------------------------------------------------------------------------
# send_text_and_submit_verified — first-attempt success
# ---------------------------------------------------------------------------


class TestSendTextVerifiedFirstAttemptSuccess:
    """Echo appears on first capture → return 1, Enter sent once."""

    def test_returns_attempt_number_one(self) -> None:
        # Arrange
        text = "hello-world"
        capture = _ScriptedCapture([f"❯ {text}\n"])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        attempt = TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=1.0,
        )
        # Assert
        assert attempt == 1

    def test_text_sent_exactly_once(self) -> None:
        # Arrange
        text = "hello-world"
        capture = _ScriptedCapture([f"❯ {text}\n"])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=1.0,
        )
        # Assert
        assert send_text.calls == [("s", text)]

    def test_enter_sent_after_echo_confirmed(self) -> None:
        # Arrange
        text = "hello-world"
        capture = _ScriptedCapture([f"❯ {text}\n"])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=1.0,
        )
        # Assert
        assert send_enter.calls == [("s", "")]


# ---------------------------------------------------------------------------
# send_text_and_submit_verified — Ink dropped first send, succeeds on retry
# ---------------------------------------------------------------------------


class _DropThenAcceptCapture:
    """Capture stub that returns an empty pane the first ``drop_count``
    times (simulating Ink eating the keystrokes) then returns the echo.

    Used by the retry-on-drop test to prove the primitive resends and
    succeeds when the TUI recovers.
    """

    def __init__(self, *, drop_count: int, echo_frame: str) -> None:
        self._drop_count = drop_count
        self._echo_frame = echo_frame
        self.calls = 0

    def __call__(self, _session: str) -> str:
        self.calls += 1
        # Each "echo window" reads several frames (poll loop). We drop
        # the FIRST send entirely by returning empty until the second
        # send happens; the simplest model is: empty for `drop_count`
        # captures, then echo.
        if self.calls <= self._drop_count:
            return ""
        return self._echo_frame


class TestSendTextVerifiedRetriesOnDrop:
    """Ink drops the first send → primitive resends and succeeds."""

    def test_returns_attempt_two_after_one_drop(self) -> None:
        # Arrange — drop ALL captures of attempt 1 (so the echo window
        # expires), accept on attempt 2.
        text = "retry-me"
        # echo_wait_s=0.01 with poll_s=0.0: one capture per attempt,
        # ample budget to retry several times.
        capture = _DropThenAcceptCapture(drop_count=1, echo_frame=f"> {text}")
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        attempt = TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=0.0,  # one capture per attempt
            max_resends=3,
        )
        # Assert
        assert attempt == 2

    def test_text_resent_for_each_attempt(self) -> None:
        # Arrange
        text = "retry-me"
        capture = _DropThenAcceptCapture(drop_count=1, echo_frame=f"> {text}")
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=0.0,
            max_resends=3,
        )
        # Assert — one send per attempt; 2 sends total here.
        assert send_text.calls == [("s", text), ("s", text)]

    def test_enter_only_sent_after_successful_echo(self) -> None:
        # Arrange
        text = "retry-me"
        capture = _DropThenAcceptCapture(drop_count=1, echo_frame=f"> {text}")
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=0.0,
            max_resends=3,
        )
        # Assert — Enter pressed exactly once, AFTER the echo on attempt 2.
        assert send_enter.calls == [("s", "")]


# ---------------------------------------------------------------------------
# send_text_and_submit_verified — exhausted retries
# ---------------------------------------------------------------------------


class TestSendTextVerifiedAllRetriesDropped:
    """Echo never appears across all retries → raise drop error."""

    def test_raises_named_exception_after_max_resends(self) -> None:
        # Arrange — capture always empty.
        text = "vanish"
        empty_capture = _ScriptedCapture([""])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act / Assert
        with pytest.raises(TuiKeystrokeDropError, match="dropped"):
            TmuxManager.send_text_and_submit_verified(
                "s",
                text,
                capture_fn=empty_capture,
                send_text_fn=send_text,
                send_enter_fn=send_enter,
                sleep_fn=_zero_sleep,
                poll_s=0.0,
                echo_wait_s=0.0,
                max_resends=2,
            )

    def test_no_enter_sent_when_all_retries_drop(self) -> None:
        # Arrange
        text = "vanish"
        empty_capture = _ScriptedCapture([""])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        try:
            TmuxManager.send_text_and_submit_verified(
                "s",
                text,
                capture_fn=empty_capture,
                send_text_fn=send_text,
                send_enter_fn=send_enter,
                sleep_fn=_zero_sleep,
                poll_s=0.0,
                echo_wait_s=0.0,
                max_resends=2,
            )
        except TuiKeystrokeDropError:
            pass
        # Assert — Enter must NOT be committed when nothing was rendered.
        assert send_enter.calls == []

    def test_total_send_count_equals_retries_plus_one(self) -> None:
        # Arrange — max_resends=2 → 3 total send attempts.
        text = "vanish"
        empty_capture = _ScriptedCapture([""])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        try:
            TmuxManager.send_text_and_submit_verified(
                "s",
                text,
                capture_fn=empty_capture,
                send_text_fn=send_text,
                send_enter_fn=send_enter,
                sleep_fn=_zero_sleep,
                poll_s=0.0,
                echo_wait_s=0.0,
                max_resends=2,
            )
        except TuiKeystrokeDropError:
            pass
        # Assert
        assert len(send_text.calls) == 3


# ---------------------------------------------------------------------------
# Echo excerpt — long text only needs a prefix match.
# ---------------------------------------------------------------------------


class TestSendTextVerifiedExcerptMatch:
    """Pane only needs to render the leading ``echo_excerpt_len`` chars."""

    def test_long_text_matches_on_excerpt_only(self) -> None:
        # Arrange — pane renders only the prefix (TUI line-wrap).
        text = "x" * 200
        capture = _ScriptedCapture(["x" * 30])
        send_text = _Recorder()
        send_enter = _Recorder()
        # Act
        attempt = TmuxManager.send_text_and_submit_verified(
            "s",
            text,
            capture_fn=capture,
            send_text_fn=send_text,
            send_enter_fn=send_enter,
            sleep_fn=_zero_sleep,
            poll_s=0.0,
            echo_wait_s=1.0,
            echo_excerpt_len=20,
        )
        # Assert
        assert attempt == 1


# EOF
