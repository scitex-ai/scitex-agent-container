"""Regression for an active turn whose submitted prompt stays in the transcript.

Observed on the live Hermes Ink TUI on compute-03 (2026-09-11): after Enter,
the submitted ``[sac-deliver:...]`` prompt remained rendered under ``❯`` while
the bottom-most ``❯`` row changed to ``Ctrl+C to interrupt…``.  The old generic
non-empty-prompt check called that state pasted-but-unsent, returned exit 1, and
invited a duplicate delivery even though the model was already reasoning.
"""

from __future__ import annotations

from typing import Optional

from scitex_agent_container._delivery import EXIT_DELIVERED, assess_delivery, deliver
from scitex_agent_container.runtimes._tui_compose import verify_submit_by_advancement

from ._helpers import TickClock


class _HermesInkPane:
    """Small stateful rendering of the observed Hermes composer boundary."""

    def __init__(self) -> None:
        self.buffer = ""
        self.submitted = ""
        self.enters = 0

    def capture(self, _target: str) -> Optional[str]:
        if self.submitted:
            return (
                "───\n"
                f"❯ {self.submitted}\n"
                "┊  Let me inspect the requested files first.\n"
                "─ ٩(๑❛ᴗ❛๑)۶ formulating… · 2s │ local model │ 12k/1m\n"
                "❯ Ctrl+C to interrupt…\n"
            )
        return f"───\n❯ {self.buffer}\n─ ready │ local model │ 12k/1m\n"

    def paste(self, _target: str, text: str) -> None:
        self.buffer += text

    def send_key(self, _target: str, key: str) -> None:
        if key != "Enter":
            return
        self.enters += 1
        self.submitted = self.buffer
        self.buffer = ""


def _deliver_to(pane: _HermesInkPane, message: str = "continue the assigned slice"):
    clock = TickClock()
    return deliver(
        "peer",
        message,
        strategy="tui",
        list_sessions_fn=lambda: ["tui-peer"],
        capture_fn=pane.capture,
        paste_fn=pane.paste,
        send_keys_fn=pane.send_key,
        arrival_timeout_s=2.0,
        idle_wait_s=2.0,
        max_resends=3,
        poll_s=0.1,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
        clock_fn=lambda: 1_800_000_000.0,
    )


def test_active_turn_with_submitted_text_in_transcript_is_verified() -> None:
    pane = _HermesInkPane()
    state = _deliver_to(pane)
    assert state.is_payload_submitted is True


def test_active_turn_with_submitted_text_returns_delivered_exit_code() -> None:
    pane = _HermesInkPane()
    verdict = assess_delivery(_deliver_to(pane))
    assert verdict.exit_code() == EXIT_DELIVERED


def test_active_turn_does_not_receive_a_duplicate_enter() -> None:
    pane = _HermesInkPane()
    _deliver_to(pane)
    assert pane.enters == 1


def test_idle_payload_mentioning_interrupt_shortcut_submits_once() -> None:
    pane = _HermesInkPane()
    _deliver_to(pane, "Document the text Ctrl+C to interrupt for operators")
    assert pane.enters == 1


def test_busy_marker_does_not_override_payload_still_in_live_composer() -> None:
    payload = "[sac-deliver:abc123def456] keep working"
    pane = f"───\n❯ {payload}\n─ Working…\n"
    clock = TickClock()
    sent: list[str] = []
    submitted = verify_submit_by_advancement(
        "peer",
        capture_fn=lambda _name: pane,
        send_keys_fn=sent.append,
        pending_fragment="abc123def456",
        appear_timeout_s=0.2,
        idle_wait_s=0.2,
        max_resends=1,
        poll_s=0.1,
        time_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert submitted is False
