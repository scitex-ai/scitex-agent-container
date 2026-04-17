"""Keystroke-timing tests for TmuxManager + ScreenManager.

The user reported a real-world bug: "intended prompt is sent to agent
terminal but not complete with Enter key failure" — the text lands in
the TUI but the submit Enter is dropped because the previous send-keys
call hasn't finished being rendered when the Enter arrives.

Two mitigations are tested here:

1. ``send_keys`` now waits ``inter_key_delay_s`` between keystrokes.
2. ``send_text_and_submit`` sends text, sleeps ``settle_s``, then sends
   Enter as a separate ``send-keys`` call so tmux interprets it as the
   Enter keyword, not a raw ``\\r`` inside the text argument.

All tests inject fake ``sleep_fn`` + patch ``subprocess.run`` so the
suite stays deterministic and fast.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scitex_agent_container.runtimes.screen import ScreenManager
from scitex_agent_container.runtimes.tmux import (
    _DEFAULT_INTER_KEY_DELAY_S,
    _DEFAULT_SUBMIT_SETTLE_S,
    TmuxManager,
)


class _SleepRecorder:
    """Stand-in for time.sleep that records the durations it was asked
    to wait, without actually waiting."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, d: float) -> None:
        self.calls.append(d)


@pytest.fixture
def sleep_rec():
    return _SleepRecorder()


class TestTmuxSendKeysInterKeyDelay:
    """send_keys must pause between keys so the TUI can re-render."""

    def test_single_key_no_sleep(self, sleep_rec):
        """One key -> zero sleeps (no key after which to wait)."""
        with patch("subprocess.run") as run:
            TmuxManager.send_keys("s1", "Enter", sleep_fn=sleep_rec)
        assert run.call_count == 1
        assert sleep_rec.calls == []

    def test_two_keys_one_sleep(self, sleep_rec):
        """Two keys -> exactly one inter-key sleep, not after the last."""
        with patch("subprocess.run") as run:
            TmuxManager.send_keys("s1", "2", "Enter", sleep_fn=sleep_rec)
        assert run.call_count == 2
        assert len(sleep_rec.calls) == 1

    def test_four_keys_three_sleeps(self, sleep_rec):
        with patch("subprocess.run") as run:
            TmuxManager.send_keys("s1", "a", "b", "c", "Enter", sleep_fn=sleep_rec)
        assert run.call_count == 4
        assert len(sleep_rec.calls) == 3

    def test_default_delay_used_when_none_passed(self, sleep_rec):
        """Delay value comes from the module default (env-overridable)."""
        with patch("subprocess.run"):
            TmuxManager.send_keys("s", "a", "b", sleep_fn=sleep_rec)
        assert sleep_rec.calls == [_DEFAULT_INTER_KEY_DELAY_S]

    def test_explicit_delay_overrides_default(self, sleep_rec):
        with patch("subprocess.run"):
            TmuxManager.send_keys(
                "s", "a", "b", "c", inter_key_delay_s=0.42, sleep_fn=sleep_rec
            )
        assert sleep_rec.calls == [0.42, 0.42]

    def test_zero_delay_skips_sleep(self, sleep_rec):
        """inter_key_delay_s=0 disables the pause (fast path for tests)."""
        with patch("subprocess.run"):
            TmuxManager.send_keys(
                "s", "a", "b", "c", inter_key_delay_s=0.0, sleep_fn=sleep_rec
            )
        assert sleep_rec.calls == []

    def test_keys_forwarded_verbatim_in_order(self, sleep_rec):
        with patch("subprocess.run") as run:
            TmuxManager.send_keys("my-sess", "2", "Enter", sleep_fn=sleep_rec)
        args_per_call = [call.args[0] for call in run.call_args_list]
        assert args_per_call[0] == ["tmux", "send-keys", "-t", "my-sess", "2"]
        assert args_per_call[1] == ["tmux", "send-keys", "-t", "my-sess", "Enter"]


class TestTmuxSendTextAndSubmit:
    """send_text_and_submit is the canonical path for 'type a message
    and press Enter' — used by startup commands and (future) nonce
    liveness probes."""

    def test_two_subprocess_calls_text_then_enter(self, sleep_rec):
        with patch("subprocess.run") as run:
            TmuxManager.send_text_and_submit("sess", "hello world", sleep_fn=sleep_rec)
        assert run.call_count == 2
        first_cmd = run.call_args_list[0].args[0]
        second_cmd = run.call_args_list[1].args[0]
        assert first_cmd == ["tmux", "send-keys", "-t", "sess", "hello world"]
        assert second_cmd == ["tmux", "send-keys", "-t", "sess", "Enter"]

    def test_settle_delay_between_text_and_enter(self, sleep_rec):
        """The whole point: a dwell between text and Enter so the TUI
        finishes rendering before the submit."""
        with patch("subprocess.run"):
            TmuxManager.send_text_and_submit("s", "msg", sleep_fn=sleep_rec)
        # Exactly one sleep, of the default settle duration.
        assert sleep_rec.calls == [_DEFAULT_SUBMIT_SETTLE_S]

    def test_custom_settle_duration(self, sleep_rec):
        with patch("subprocess.run"):
            TmuxManager.send_text_and_submit(
                "s", "msg", settle_s=0.75, sleep_fn=sleep_rec
            )
        assert sleep_rec.calls == [0.75]

    def test_zero_settle_skips_sleep_but_still_sends_enter(self, sleep_rec):
        """settle=0 is allowed (for callers that manage their own
        pacing), but Enter still fires as a separate subprocess call."""
        with patch("subprocess.run") as run:
            TmuxManager.send_text_and_submit("s", "msg", settle_s=0, sleep_fn=sleep_rec)
        assert sleep_rec.calls == []
        assert run.call_count == 2

    def test_does_not_append_cr_to_text(self, sleep_rec):
        """Text is sent unchanged — no ``\\r`` / ``\\n`` appended —
        because the separate Enter call is the canonical submit.
        Regression guard: appending ``\\r`` was the old behavior that
        caused dropped submits."""
        with patch("subprocess.run") as run:
            TmuxManager.send_text_and_submit(
                "s", "multi word prompt", sleep_fn=sleep_rec
            )
        text_arg = run.call_args_list[0].args[0][-1]
        assert text_arg == "multi word prompt"
        assert not text_arg.endswith("\r")
        assert not text_arg.endswith("\n")


class TestScreenSendKeysInterKeyDelay:
    """ScreenManager mirrors the tmux behavior but via ``screen -X stuff``."""

    def test_inter_key_delay_between_stuffs(self, sleep_rec):
        with patch("subprocess.run") as run:
            ScreenManager.send_keys("s", "a", "b", "c", sleep_fn=sleep_rec)
        assert run.call_count == 3
        assert len(sleep_rec.calls) == 2

    def test_explicit_delay_used(self, sleep_rec):
        with patch("subprocess.run"):
            ScreenManager.send_keys(
                "s", "a", "b", inter_key_delay_s=0.25, sleep_fn=sleep_rec
            )
        assert sleep_rec.calls == [0.25]


class TestScreenSendTextAndSubmit:
    """Screen uses ``\\r`` as the submit byte (no Enter keyword)."""

    def test_submit_byte_is_carriage_return(self, sleep_rec):
        with patch("subprocess.run") as run:
            ScreenManager.send_text_and_submit("s", "hello", sleep_fn=sleep_rec)
        assert run.call_count == 2
        first_stuff = run.call_args_list[0].args[0][-1]
        second_stuff = run.call_args_list[1].args[0][-1]
        assert first_stuff == "hello"
        assert second_stuff == "\r"

    def test_settle_delay_applied(self, sleep_rec):
        with patch("subprocess.run"):
            ScreenManager.send_text_and_submit(
                "s", "hello", settle_s=0.5, sleep_fn=sleep_rec
            )
        assert sleep_rec.calls == [0.5]


class TestKeystrokeTimingRegression:
    """Pin the behavior that specifically addresses the user's bug
    report. These tests fail if someone accidentally restores the old
    zero-delay / in-text-CR behavior."""

    def test_numbered_prompt_sequence_paces_the_enter(self, sleep_rec):
        """The bypass-permissions / dev-channels / thinking-effort
        prompts all fire ``["<digit>", "Enter"]`` via send_keys. Pin
        that a delay lands between the digit and the Enter."""
        with patch("subprocess.run"):
            TmuxManager.send_keys("s", "2", "Enter", sleep_fn=sleep_rec)
        assert len(sleep_rec.calls) == 1
        assert sleep_rec.calls[0] > 0

    def test_startup_command_uses_separate_enter_not_embedded_cr(self, sleep_rec):
        """The startup-commands path used to append ``\\r`` to the text
        and send it in a single ``send_keys`` call, which dropped the
        submit on busy TUIs. ``send_text_and_submit`` now sends the
        text and the Enter as two separate subprocess calls with a
        settle in between — verify that contract."""
        with patch("subprocess.run") as run:
            TmuxManager.send_text_and_submit(
                "s", "/compact", settle_s=0.1, sleep_fn=sleep_rec
            )
        # Two separate subprocess calls — text, then Enter — with a
        # settle-sleep in between.
        assert run.call_count == 2
        assert sleep_rec.calls == [0.1]
        assert run.call_args_list[1].args[0][-1] == "Enter"
