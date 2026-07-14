"""Unit tests for the BUG 3 fresh-boot-welcome-screen guard in
``clear_compose_buffer`` / ``is_fresh_boot_welcome_screen``.

Card ``sac-tui-clear-compose-buffer-on-boot``: on a FRESH boot (no prior
session, ``--continue`` omitted) Claude Code's first-launch welcome/
model-info/promo screen can still be up — with the Ink TUI's input not yet
bound — when the Escape-based compose-buffer clear runs. Reproduced live
2026-07-05 and 2026-07-08 (scitex-todo, scitex-db, scitex-session, scitex-io,
figrecipe, scitex-stats, paper-neurovista): "stale compose buffer ... did NOT
clear after 5 attempts of ['Escape', 'Escape']" against a pane showing the
welcome banner + a "Fable 5 is included in your weekly limit" promo line.
ONLY fresh/no-transcript boots showed this; resumed-session boots never did.

Real recording fakes (no mocks / no monkeypatch — STX-NM002) + a fake
monotonic clock so the bounded-wait paths run instantly. AAA markers, one
assert each (STX-TQ002 / STX-TQ007).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scitex_agent_container.runtimes._tui_compose import (
    _COMPOSE_CLEAR_KEYS,
    clear_compose_buffer,
    is_fresh_boot_welcome_screen,
)

# ── Real(istic) pane snapshots ──────────────────────────────────────────────

# FRESH boot: first-launch welcome/model-info box + the exact promo-banner
# line from the live 2026-07-08 repro on this card, reconstructed on the box
# shape CONFIRMED real by `_V2_READY_PANE` (test_tui_session_v2_ready_marker.py,
# captured live 2026-06-20) with "Welcome back Yusuke!" swapped for the
# first-launch greeting.
_FRESH_BOOT_WELCOME_PANE = (
    "╭─── Claude Code v2.1.198 ───╮\n"
    "│   Welcome to Claude Code!  │\n"
    "╰─────────────────────────────╯\n"
    "  Fable 5 is included in your weekly limit\n"
    "\n"
    "❯ Try \"fix the failing tests\"\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)

# RESUMED session: the CONFIRMED real capture's own greeting variant — must
# stay untouched by this guard (the already-working resumed boot path).
_RESUMED_WELCOME_PANE = (
    "╭─── Claude Code v2.1.150 ───╮\n"
    "│      Welcome back Yusuke!  │  What's new\n"
    "╰────────────────────────────╯\n"
    "❯ \n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)

_BOOTING_PANE = "uv pip install -e .[all,dev]\nResolving dependencies ..."

_DEV_CHANNELS_MODAL = (
    "1. I am using this for local development\n2. Exit\nEnter to confirm"
)

# Once the welcome screen clears, the real live box underneath — empty, the
# common fresh-boot case (nothing was ever pasted into it yet).
_CLEARED = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"

# Once the welcome screen clears, a GENUINELY stale multi-line buffer sitting
# in the persistent tmux pane (the ORIGINAL /compact-burst bug this whole
# mechanism exists for) — must still be clearable once the welcome screen is
# out of the way.
_STALE_MULTILINE = (
    "❯\xa0/compact\n"
    "  /compact\n"
    "  2\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


# ── Fakes (real callables, not mocks) ───────────────────────────────────────


@dataclass
class _RecordingSend:
    keys: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.keys.append(key)


@dataclass
class _FakeClock:
    """Deterministic ``time_fn``/``sleep_fn`` pair: ``sleep`` advances ``now``
    synthetically so a bounded wall-clock wait resolves instantly in tests,
    no real sleeping required."""

    now: float = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return self.now


class _WelcomeThenAfter:
    """capture_fn: the fresh-boot welcome pane for the first
    ``welcome_captures`` calls, then ``after`` for every call thereafter.
    Models the Ink TUI mounting its real input once the welcome/promo screen
    finishes its render window."""

    def __init__(self, welcome_captures: int, after: str) -> None:
        self._remaining = welcome_captures
        self._after = after
        self.captures = 0

    def __call__(self, _name: str) -> str:
        self.captures += 1
        if self._remaining > 0:
            self._remaining -= 1
            return _FRESH_BOOT_WELCOME_PANE
        return self._after


class _WelcomeThenStaleUntilCleared:
    """capture_fn: welcome pane for the first ``welcome_captures`` calls,
    then the REAL stale multiline buffer until ``release_after_gestures``
    full Escape-Escape gestures have been sent, then cleared."""

    def __init__(
        self, sender: _RecordingSend, *, welcome_captures: int, release_after_gestures: int = 1
    ) -> None:
        self._sender = sender
        self._welcome_captures = welcome_captures
        self._release = release_after_gestures
        self.captures = 0

    def __call__(self, _name: str) -> str:
        self.captures += 1
        if self.captures <= self._welcome_captures:
            return _FRESH_BOOT_WELCOME_PANE
        gestures = len(self._sender.keys) // len(_COMPOSE_CLEAR_KEYS)
        return _CLEARED if gestures >= self._release else _STALE_MULTILINE


class _AlwaysWelcome:
    """capture_fn: the welcome screen never clears (pathological slow/dead
    mount) — the worst case this guard must still fail loud on, not hang."""

    def __call__(self, _name: str) -> str:
        return _FRESH_BOOT_WELCOME_PANE


# ═══════════════════════════════════════════════════════════════════════════
# is_fresh_boot_welcome_screen — the predicate itself
# ═══════════════════════════════════════════════════════════════════════════


def test_predicate_matches_fresh_boot_welcome_pane() -> None:
    # Arrange
    pane = _FRESH_BOOT_WELCOME_PANE
    # Act
    matched = is_fresh_boot_welcome_screen(pane)
    # Assert
    assert matched is True


def test_predicate_does_not_match_resumed_welcome_back_pane() -> None:
    # Arrange — the already-working resumed path must stay untouched.
    pane = _RESUMED_WELCOME_PANE
    # Act
    matched = is_fresh_boot_welcome_screen(pane)
    # Assert
    assert matched is False


def test_predicate_does_not_match_plain_booting_pane() -> None:
    # Arrange
    pane = _BOOTING_PANE
    # Act
    matched = is_fresh_boot_welcome_screen(pane)
    # Assert
    assert matched is False


def test_predicate_does_not_match_dev_channels_modal() -> None:
    # Arrange — no accidental overlap with an unrelated known modal.
    pane = _DEV_CHANNELS_MODAL
    # Act
    matched = is_fresh_boot_welcome_screen(pane)
    # Assert
    assert matched is False


# ═══════════════════════════════════════════════════════════════════════════
# clear_compose_buffer — waits out the welcome screen, then no-ops on an
# actually-empty live box (the common fresh-boot case)
# ═══════════════════════════════════════════════════════════════════════════


def test_clear_returns_true_once_welcome_screen_clears_to_empty_box() -> None:
    # Arrange — welcome screen for 2 captures, then the real (empty) box.
    clock = _FakeClock()
    capture = _WelcomeThenAfter(welcome_captures=2, after=_CLEARED)
    sender = _RecordingSend()
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.1,
        welcome_wait_s=2.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert
    assert ok is True


def test_clear_sends_no_escape_while_welcome_screen_is_up() -> None:
    # Arrange — same scenario: nothing to clear once the real box is seen.
    clock = _FakeClock()
    capture = _WelcomeThenAfter(welcome_captures=2, after=_CLEARED)
    sender = _RecordingSend()
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.1,
        welcome_wait_s=2.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert — no Escape was ever sent into the welcome screen.
    assert sender.keys == []


# ═══════════════════════════════════════════════════════════════════════════
# clear_compose_buffer — waits out the welcome screen, THEN still clears a
# GENUINE stale buffer underneath (the original /compact-burst fix preserved)
# ═══════════════════════════════════════════════════════════════════════════


def test_clear_still_clears_real_stale_buffer_once_welcome_screen_is_gone() -> None:
    # Arrange — welcome screen for 1 capture, then a real stale stack that
    # clears on the first Escape-Escape gesture.
    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _WelcomeThenStaleUntilCleared(
        sender, welcome_captures=1, release_after_gestures=1
    )
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.1,
        welcome_wait_s=2.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert
    assert ok is True


def test_clear_sends_the_clear_gesture_only_after_welcome_screen_clears() -> None:
    # Arrange — same scenario as above.
    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _WelcomeThenStaleUntilCleared(
        sender, welcome_captures=1, release_after_gestures=1
    )
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=5,
        poll_s=0.1,
        welcome_wait_s=2.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert — exactly one verified clear gesture, sent once the screen was gone.
    assert sender.keys == list(_COMPOSE_CLEAR_KEYS)


# ═══════════════════════════════════════════════════════════════════════════
# clear_compose_buffer — welcome screen NEVER clears: bounded, fail-loud, not
# fatal (no worse than the pre-fix worst case; must not hang)
# ═══════════════════════════════════════════════════════════════════════════


def test_clear_returns_false_when_welcome_screen_never_clears() -> None:
    # Arrange — pathological: the welcome screen is stuck forever.
    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _AlwaysWelcome()
    # Act
    ok = clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=3,
        poll_s=0.1,
        welcome_wait_s=1.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert — bounded give-up, never a silent/blocking hang.
    assert ok is False


def test_clear_sends_no_escape_when_welcome_screen_never_clears() -> None:
    # Arrange — same pathological scenario.
    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _AlwaysWelcome()
    # Act
    clear_compose_buffer(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_attempts=3,
        poll_s=0.1,
        welcome_wait_s=1.0,
        sleep_fn=clock.sleep,
        time_fn=clock.time,
    )
    # Assert — never blind-fires Escape into a screen that can't consume it.
    assert sender.keys == []


def test_clear_does_not_raise_when_welcome_screen_never_clears() -> None:
    # Arrange — fail-loud-not-fatal: boot must still proceed.
    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _AlwaysWelcome()
    # Act
    raised = False
    try:
        clear_compose_buffer(
            "tui-x",
            capture_fn=capture,
            send_keys_fn=sender,
            max_attempts=3,
            poll_s=0.1,
            welcome_wait_s=1.0,
            sleep_fn=clock.sleep,
            time_fn=clock.time,
        )
    except Exception:  # noqa: BLE001 — the whole point is nothing escapes.
        raised = True
    # Assert
    assert raised is False


def test_clear_logs_welcome_specific_message_when_screen_never_clears(caplog) -> None:
    # Arrange — the exhaustion message must name the ACTUAL cause (the
    # welcome screen never releasing), not misreport "Ink TUI kept dropping
    # the clear keystroke" when no keystroke was ever sent.
    import logging

    clock = _FakeClock()
    sender = _RecordingSend()
    capture = _AlwaysWelcome()
    # Act
    with caplog.at_level(logging.ERROR):
        clear_compose_buffer(
            "tui-x",
            capture_fn=capture,
            send_keys_fn=sender,
            max_attempts=3,
            poll_s=0.1,
            welcome_wait_s=1.0,
            sleep_fn=clock.sleep,
            time_fn=clock.time,
        )
    # Assert
    assert any("welcome" in r.message.lower() for r in caplog.records)
