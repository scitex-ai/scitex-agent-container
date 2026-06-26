"""Regression: /compact-burst-on-restart (sac-tui-clear-compose-buffer-on-boot).

On an agent RESTART the persistent tmux pane can carry a BURST of stale
"compose-pending-unsent" text (observed live: 9× ``/compact`` + a stray ``2``)
that EXTERNAL input accumulated in the Ink TUI compose box while the pane was
busy. The boot's startup-prompt injection used to paste + submit WITHOUT first
clearing that stale buffer, so the boot Enter submitted the whole stale stack.

The fix (:func:`clear_compose_buffer`) empties the live compose box BEFORE the
per-prompt paste/submit loop:

  * no-op when the live box is already empty (the COMMON case — one capture,
    no keystroke);
  * otherwise send the EMPIRICALLY-verified clear keystrokes (double
    ``Escape`` — clears a multi-line buffer without submitting or quitting,
    confirmed against a real claude 2.1.150 TUI, 2026-06-26), then poll until
    the box is empty, bounded by ``max_attempts``;
  * fail-loud-not-fatal: on exhaustion log LOUD and return False, never raise
    (boot must proceed; verify_submit_by_advancement is the second net).

These tests drive the pure function with REAL fake callables (a scripted /
state-machine ``capture_fn`` + a recording ``send_keys_fn``) — no MagicMock,
no monkeypatch-as-fixture-param (PA-306). STX-TQ002 AAA each on its own line,
STX-TQ007 one assertion per test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scitex_agent_container.runtimes._tui_compose import (
    _COMPOSE_CLEAR_KEYS,
    clear_compose_buffer,
)

# ── Realistic pane snapshots ────────────────────────────────────────────────
# Only the load-bearing glyphs matter to _compose_pending_live (the bottom-most
# ❯ row): text after the live ❯ == pending; an empty live box == cleared.

# A multi-line stale stack sitting unsent in the live compose box — exactly the
# /compact-burst the operator observed across a restart.
_STALE_MULTILINE = (
    "❯\xa0/compact\n"
    "  /compact\n"
    "  /compact\n"
    "  2\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
# Cleared: the live ❯ box is empty (placeholder), ready for the boot paste.
_CLEARED = (
    "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


# ── Fakes (real callables, not mocks) ───────────────────────────────────────


@dataclass
class _RecordingSend:
    """A ``send_keys_fn`` that records every key sent."""

    keys: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.keys.append(key)


@dataclass
class _ScriptedPane:
    """A ``capture_fn`` returning a scripted sequence of pane snapshots.

    Each call returns the next snapshot; once exhausted it keeps returning the
    LAST one (a steady terminal state).
    """

    snapshots: list[str]
    captures: int = 0

    def __call__(self, _name: str) -> str:
        idx = min(self.captures, len(self.snapshots) - 1)
        self.captures += 1
        return self.snapshots[idx]


class _StaleUntilCleared:
    """State-machine ``capture_fn``: reports the multi-line stale stack until
    the paired sender has sent ``release`` full clear-key gestures, then
    reports the cleared (empty) box.

    Models the real TUI: the stale buffer survives until the clear keystrokes
    actually LAND; ``release`` lets a test simulate the Ink TUI eating the
    first N gestures before one finally clears.
    """

    def __init__(self, sender: _RecordingSend, *, release_after_gestures: int = 1) -> None:
        self._sender = sender
        self._release = release_after_gestures

    def __call__(self, _name: str) -> str:
        # One "gesture" == one full _COMPOSE_CLEAR_KEYS sequence. Count COMPLETE
        # gestures (a key in the sequence repeats, so counting a single key is
        # wrong — divide the total sent by the gesture length).
        gestures = len(self._sender.keys) // len(_COMPOSE_CLEAR_KEYS)
        return _CLEARED if gestures >= self._release else _STALE_MULTILINE


def _no_sleep(_s: float) -> None:
    return None


# ── (a) stale pending buffer -> helper clears it ─────────────────────────────


def test_clears_stale_pending_buffer_and_returns_true() -> None:
    # Arrange — the live box holds a multi-line stale stack; the clear gesture
    # empties it on the first try.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=1)
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — cleared successfully.
    assert ok is True


def test_sends_the_verified_clear_keystrokes_when_buffer_is_stale() -> None:
    # Arrange — stale multi-line buffer, cleared after one gesture.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=1)
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — exactly the empirically-verified clear gesture was sent.
    assert sender.keys == list(_COMPOSE_CLEAR_KEYS)


# ── (b) already-empty buffer -> no-op (no keystroke sent) ────────────────────


def test_noop_when_buffer_already_empty_sends_no_keys() -> None:
    # Arrange — the live box is already empty (the common fresh-boot case).
    capture = _ScriptedPane([_CLEARED])
    sender = _RecordingSend()
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — no-op: cleared (nothing to do) and not a single key sent.
    assert (ok, sender.keys) == (True, [])


def test_noop_captures_only_once_when_already_empty() -> None:
    # Arrange — already empty: the helper should short-circuit after one read.
    capture = _ScriptedPane([_CLEARED])
    sender = _RecordingSend()
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — exactly one capture, no polling loop entered.
    assert capture.captures == 1


# ── (c) clear never succeeds -> returns False, logs loud, does NOT raise ──────


def test_returns_false_when_clear_never_succeeds() -> None:
    # Arrange — the Ink TUI drops every clear gesture (release impossibly high).
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=999)
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=3,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — bounded give-up returns False (never silently True).
    assert ok is False


def test_bounded_attempts_does_not_exceed_max_attempts() -> None:
    # Arrange — same never-clearing pane; assert the resend bound holds.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=999)
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=2,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — one full clear gesture per attempt, never more than the bound.
    assert sender.keys == list(_COMPOSE_CLEAR_KEYS) * 2


def test_logs_loud_on_exhaustion(caplog) -> None:
    # Arrange — never clears, so the exhaustion path fires.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=999)
    # Act
    with caplog.at_level(logging.ERROR):
        clear_compose_buffer(
            "tui-x",
            capture_fn=capture,
            send_keys_fn=sender,
            max_attempts=2,
            poll_s=0.0,
            sleep_fn=_no_sleep,
        )
    # Assert — a LOUD error was logged (not a silent best-effort return).
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_does_not_raise_when_clear_never_succeeds() -> None:
    # Arrange — never clears; boot must still proceed, so no exception.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=999)
    # Act
    raised = False
    try:
        clear_compose_buffer(
            "tui-x",
            capture_fn=capture,
            send_keys_fn=sender,
            max_attempts=2,
            poll_s=0.0,
            sleep_fn=_no_sleep,
        )
    except Exception:  # noqa: BLE001 — the whole point is that NOTHING escapes.
        raised = True
    # Assert — fail-loud-not-fatal: it returns, never raises.
    assert raised is False


# ── (d) resends across a dropped gesture, then clears ────────────────────────


def test_resends_clear_when_first_gesture_is_dropped() -> None:
    # Arrange — the Ink TUI eats the FIRST clear gesture; the second lands.
    sender = _RecordingSend()
    capture = _StaleUntilCleared(sender, release_after_gestures=2)
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.0,
        sleep_fn=_no_sleep,
    )
    # Assert — resent until cleared (two full gestures), then succeeded.
    assert (ok, sender.keys) == (True, list(_COMPOSE_CLEAR_KEYS) * 2)
