"""Unit tests for the BUG 1 Esc-cancel guard in ``clear_compose_buffer``.

Card sac-boot-automation-devchannels-modal-continue-compose-buffer, BUG 1: the
compose-buffer clear sends ``Escape`` — but while a dev-channels / "Esc to
cancel" modal is on screen, an Escape CANCELS the launch and KILLS the tmux
session. ``clear_compose_buffer`` MUST refuse to send Escape while such a modal
is present.

Real recording fakes (no mocks / no monkeypatch — STX-NM002). AAA markers,
one assert each.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._tui_compose import clear_compose_buffer

_DEVCHAN_WITH_PENDING = (
    "❯ some stale pending text\n"
    "1. I am using this for local development\n"
    "Enter to confirm · Esc to cancel"
)
_CLEAN_PENDING = "❯ some stale pending text that should be cleared"


class _Recorder:
    """Real capture/send stand-in: fixed pane, records sent keys."""

    def __init__(self, pane: str) -> None:
        self._pane = pane
        self.sent: list[str] = []

    def capture(self, _name: str) -> str:
        return self._pane

    def send(self, key: str) -> None:
        self.sent.append(key)


def _noop_sleep(_s: float) -> None:
    return None


def test_clear_refuses_escape_while_dev_channels_modal_present() -> None:
    # Arrange — a cancelable modal is on screen alongside pending text.
    rec = _Recorder(_DEVCHAN_WITH_PENDING)
    # Act
    clear_compose_buffer(
        "s",
        capture_fn=rec.capture,
        send_keys_fn=rec.send,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_noop_sleep,
    )
    # Assert — NO Escape sent (an Escape here would kill the session).
    assert rec.sent == []


def test_clear_returns_false_when_cancelable_modal_present() -> None:
    # Arrange
    rec = _Recorder(_DEVCHAN_WITH_PENDING)
    # Act
    result = clear_compose_buffer(
        "s",
        capture_fn=rec.capture,
        send_keys_fn=rec.send,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_noop_sleep,
    )
    # Assert — refuses (False), boot proceeds via the reordered drain.
    assert result is False


def test_clear_sends_escape_when_no_cancelable_modal() -> None:
    # Arrange — pending text, NO cancelable modal (the safe-to-Esc case).
    rec = _Recorder(_CLEAN_PENDING)
    # Act
    clear_compose_buffer(
        "s",
        capture_fn=rec.capture,
        send_keys_fn=rec.send,
        max_attempts=1,
        poll_s=0.0,
        sleep_fn=_noop_sleep,
    )
    # Assert — the double-Escape clear ran (safe: no cancelable modal up).
    assert rec.sent == ["Escape", "Escape"]
