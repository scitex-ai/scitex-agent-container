"""Unit tests for the pure modal-drain primitives (``runtimes/_tui_drain``).

Boot-automation fix (card
sac-boot-automation-devchannels-modal-continue-compose-buffer): the drain must
dismiss the dev-channels ("Esc to cancel") modal by its REGISTERED keys
(Enter/digit, NEVER Escape — BUG 1) and must SETTLE the pane before sending so a
large ``--continue`` replay's re-render does not drop the keys (BUG 2).

All fakes are real callables recording their inputs (no mocks / no monkeypatch —
STX-NM002). AAA markers, one assert each, ``test__<module>.py`` name.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._tui_drain import (
    drain_modals_until_ready,
    wait_for_settle,
)

# ---------------------------------------------------------------------------
# Scripted-pane fake: returns successive pane contents, records sent keys.
# ---------------------------------------------------------------------------


class _ScriptedPane:
    """Real capture/send stand-in backed by a scripted list of pane frames.

    ``frames`` is consumed one per capture; the LAST frame repeats once the
    script is exhausted (so a loop that keeps polling sees a stable pane).
    ``sent`` records every key sent, in order — the assertion surface for the
    "never Escape while dev-channels up" property.
    """

    def __init__(self, frames: list[str], *, alive: bool = True) -> None:
        self._frames = list(frames)
        self._idx = 0
        self._alive = alive
        self.sent: list[str] = []

    def capture(self, _name: str) -> str:
        frame = self._frames[min(self._idx, len(self._frames) - 1)]
        if self._idx < len(self._frames) - 1:
            self._idx += 1
        return frame

    def send(self, key: str) -> None:
        self.sent.append(key)

    def exists(self, _name: str) -> bool:
        return self._alive


_DEVCHAN = (
    "❯ 1. I am using this for local development\n"
    "  2. Exit\n"
    "Enter to confirm · Esc to cancel"
)
_READY = "some output\nbypass permissions"


def _noop_sleep(_s: float) -> None:
    return None


# ---------------------------------------------------------------------------
# wait_for_settle — returns once the pane stops changing for quiet_s.
# ---------------------------------------------------------------------------


def test_wait_for_settle_returns_after_content_stabilises() -> None:
    # Arrange — pane changes twice then holds steady.
    pane = _ScriptedPane(["a", "b", "c", "c", "c"])
    clock = iter([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5])
    # Act
    result = wait_for_settle(
        "s",
        capture_fn=pane.capture,
        quiet_s=1.0,
        max_wait_s=100.0,
        poll_s=0.5,
        sleep_fn=_noop_sleep,
        time_fn=lambda: next(clock),
    )
    # Assert
    assert result == "c"


# ---------------------------------------------------------------------------
# BUG 1 — the drain NEVER sends Escape while dev-channels is on screen.
# ---------------------------------------------------------------------------


def test_drain_never_sends_escape_for_dev_channels_modal() -> None:
    # Arrange — dev-channels up, then ready after the confirm keys land.
    pane = _ScriptedPane([_DEVCHAN, _DEVCHAN, _READY])
    # Act
    drain_modals_until_ready(
        "s",
        capture_fn=pane.capture,
        send_keys_fn=pane.send,
        exists_fn=pane.exists,
        timeout_s=100.0,
        poll_s=0.0,
        settle_quiet_s=0.0,
        settle_max_s=0.0,
        sleep_fn=_noop_sleep,
        time_fn=iter([float(i) for i in range(200)]).__next__,
    )
    # Assert — Escape must never appear in the sent keys.
    assert "Escape" not in pane.sent


def test_drain_confirms_dev_channels_with_enter() -> None:
    # Arrange — dev-channels modal held stable through the settle, then ready
    # after the confirm keys land.
    pane = _ScriptedPane([_DEVCHAN, _DEVCHAN, _DEVCHAN, _READY])
    # Act
    drain_modals_until_ready(
        "s",
        capture_fn=pane.capture,
        send_keys_fn=pane.send,
        exists_fn=pane.exists,
        timeout_s=100.0,
        poll_s=0.0,
        settle_quiet_s=0.0,
        settle_max_s=0.0,
        sleep_fn=_noop_sleep,
        time_fn=iter([float(i) for i in range(200)]).__next__,
    )
    # Assert — dev-channels registered keys are ["1", "Enter"].
    assert pane.sent == ["1", "Enter"]


# ---------------------------------------------------------------------------
# Fail-fast on session death (a dead session can never reach ready).
# ---------------------------------------------------------------------------


def test_drain_aborts_false_when_session_dead() -> None:
    # Arrange — session already gone.
    pane = _ScriptedPane([""], alive=False)
    # Act
    result = drain_modals_until_ready(
        "s",
        capture_fn=pane.capture,
        send_keys_fn=pane.send,
        exists_fn=pane.exists,
        timeout_s=100.0,
        poll_s=0.0,
        settle_quiet_s=0.0,
        settle_max_s=0.0,
        sleep_fn=_noop_sleep,
        time_fn=iter([float(i) for i in range(200)]).__next__,
    )
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# Ready marker → immediate success (no keys sent).
# ---------------------------------------------------------------------------


def test_drain_returns_true_when_already_ready() -> None:
    # Arrange
    pane = _ScriptedPane([_READY])
    # Act
    result = drain_modals_until_ready(
        "s",
        capture_fn=pane.capture,
        send_keys_fn=pane.send,
        exists_fn=pane.exists,
        timeout_s=100.0,
        poll_s=0.0,
        settle_quiet_s=0.0,
        settle_max_s=0.0,
        sleep_fn=_noop_sleep,
        time_fn=iter([float(i) for i in range(200)]).__next__,
    )
    # Assert
    assert result is True
