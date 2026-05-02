"""Tests for auto-accept layers 1-4.

Coverage:
- respond() for all state-action mappings
- y_n_prompt verification (positive + negative)
- throttle: two send-accept calls within 5 s → second is no-op
- Daemon tick with monkeypatched clock
- pane_capture: returns str on error
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Layer 3 — respond()
# ---------------------------------------------------------------------------


class TestRespond:
    def _make_calls(self):
        sent_keys: list = []
        dmed: list = []
        send_fn = lambda *keys: sent_keys.extend(keys)
        dm_fn = lambda ch, msg: dmed.append((ch, msg))
        return sent_keys, dmed, send_fn, dm_fn

    def test_compose_pending_unsent_sends_enter(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        result = respond("agent1", "compose_pending_unsent", "", send_fn=sfn, dm_fn=dfn)
        assert result is True
        assert "Enter" in sent
        assert dmed == []

    def test_y_n_prompt_with_yes_option_sends_keys(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        pane = "Do you want to proceed?\n[1] Yes\n[2] No"
        result = respond("agent1", "y_n_prompt", pane, send_fn=sfn, dm_fn=dfn)
        assert result is True
        assert "1" in sent
        assert "Enter" in sent
        assert dmed == []

    def test_y_n_prompt_without_yes_option_is_noop(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        pane = "Do you want to proceed?\n[1] No\n[2] Cancel"
        result = respond("agent1", "y_n_prompt", pane, send_fn=sfn, dm_fn=dfn)
        assert result is False
        assert sent == []

    def test_auth_error_escalates_to_mgr_auth(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        result = respond("agent1", "auth_error", "", send_fn=sfn, dm_fn=dfn)
        assert result is False
        assert sent == []
        assert any(ch == "mgr-auth" for ch, _ in dmed)

    def test_limit_reached_escalates_to_healer(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        result = respond("agent1", "limit_reached", "", send_fn=sfn, dm_fn=dfn)
        assert result is False
        assert sent == []
        assert any(ch == "healer" for ch, _ in dmed)

    def test_unknown_state_is_noop(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        result = respond("agent1", "unknown", "", send_fn=sfn, dm_fn=dfn)
        assert result is False
        assert sent == []
        assert dmed == []

    def test_running_state_is_noop(self):
        from scitex_agent_container.auto.accept import respond

        sent, dmed, sfn, dfn = self._make_calls()
        result = respond("agent1", "running", "", send_fn=sfn, dm_fn=dfn)
        assert result is False
        assert sent == []


# ---------------------------------------------------------------------------
# y_n_prompt verification edge cases
# ---------------------------------------------------------------------------


class TestYNVerification:
    def _respond(self, pane):
        from scitex_agent_container.auto.accept import respond

        sent = []
        result = respond(
            "a",
            "y_n_prompt",
            pane,
            send_fn=lambda *k: sent.extend(k),
            dm_fn=lambda *_: None,
        )
        return result, sent

    @pytest.mark.parametrize(
        "pane",
        [
            "[1] Yes\n[2] No",
            "1. Yes  proceed",
            "1) Yes, continue",
        ],
    )
    def test_positive_yes_variants(self, pane):
        result, sent = self._respond(pane)
        assert result is True
        assert "1" in sent

    @pytest.mark.parametrize(
        "pane",
        [
            "[1] No\n[2] Cancel",
            "1. Proceed\n2. Abort",
            "",
        ],
    )
    def test_negative_no_yes_option(self, pane):
        result, sent = self._respond(pane)
        assert result is False
        assert sent == []


# ---------------------------------------------------------------------------
# Throttle: two send-accept calls within 5 s → second is no-op
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_second_call_within_5s_is_noop(self):
        from scitex_agent_container.auto.daemon import run_daemon

        calls: list[str] = []
        sleeps: list[float] = []
        tick_count = 0

        def fake_capture(name):
            return "❯ some text"

        def fake_classify(text):
            return ("compose_pending_unsent", "")

        def fake_respond(name, state, pane):
            calls.append(state)
            return True

        def fake_sleep(s):
            nonlocal tick_count
            sleeps.append(s)
            tick_count += 1
            if tick_count >= 2:
                import os as _os
                import signal as _sig
                _os.kill(_os.getpid(), _sig.SIGTERM)

        with patch("scitex_agent_container.auto.daemon.write_pid"):
            with patch("scitex_agent_container.auto.daemon.clear_pid"):
                run_daemon(
                    "test-agent",
                    tick_s=1.0,
                    min_send_interval_s=5.0,
                    unchanged_wait_s=0.1,
                    capture_fn=fake_capture,
                    classify_fn=fake_classify,
                    respond_fn=fake_respond,
                    sleep_fn=fake_sleep,
                )

        # First call: state changes → respond called
        # Second iteration: same state → unchanged_wait_s sleep, no respond
        assert len(calls) == 1, f"Expected 1 respond call, got {len(calls)}: {calls}"
        assert any(s <= 1.0 for s in sleeps)


# ---------------------------------------------------------------------------
# Daemon tick with monkeypatched clock
# ---------------------------------------------------------------------------


class TestDaemonTick:
    def test_daemon_stops_on_sigterm(self):
        from scitex_agent_container.auto.daemon import run_daemon

        iterations = []

        def fake_capture(name):
            return ""

        def fake_classify(text):
            return ("unknown", "")

        def fake_respond(name, state, pane):
            return False

        call_count = 0

        def fake_sleep(s):
            nonlocal call_count
            call_count += 1
            iterations.append(s)
            if call_count >= 1:
                import os as _os
                import signal as _sig
                _os.kill(_os.getpid(), _sig.SIGTERM)

        with patch("scitex_agent_container.auto.daemon.write_pid"):
            with patch("scitex_agent_container.auto.daemon.clear_pid"):
                run_daemon(
                    "test-daemon",
                    tick_s=60.0,
                    capture_fn=fake_capture,
                    classify_fn=fake_classify,
                    respond_fn=fake_respond,
                    sleep_fn=fake_sleep,
                )

        assert call_count >= 1


# ---------------------------------------------------------------------------
# Layer 1 — pane_capture returns str on error
# ---------------------------------------------------------------------------


class TestPaneCapture:
    def test_returns_empty_on_missing_session(self):
        from scitex_agent_container.runtimes.pane_capture import pane_capture

        # A session that definitely doesn't exist; tmux returns nonzero
        result = pane_capture("__nonexistent_agent_xyz__")
        assert isinstance(result, str)

    def test_truncates_to_max_chars(self):
        from scitex_agent_container.runtimes.pane_capture import pane_capture

        long_text = "x" * 20_000
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=long_text)
            result = pane_capture("agent", max_chars=100)
        assert len(result) <= 100


# ---------------------------------------------------------------------------
# prompts.py — compose_pending_unsent handler
# ---------------------------------------------------------------------------


class TestComposePendingHandler:
    def test_detect_compose_pending(self):
        from scitex_agent_container.runtimes.prompts import (
            _detect_compose_pending_unsent,
        )

        assert _detect_compose_pending_unsent("❯ hello world")
        assert not _detect_compose_pending_unsent("❯  ")  # only whitespace after ❯
        assert not _detect_compose_pending_unsent("no prompt here")

    def test_handler_registered_in_list(self):
        from scitex_agent_container.runtimes.prompts import PROMPT_HANDLERS

        names = [h.name for h in PROMPT_HANDLERS]
        assert "compose-pending-unsent" in names

    def test_handler_sends_enter(self):
        from scitex_agent_container.runtimes.prompts import PROMPT_HANDLERS

        handler = next(h for h in PROMPT_HANDLERS if h.name == "compose-pending-unsent")
        assert handler.keys == ["Enter"]
