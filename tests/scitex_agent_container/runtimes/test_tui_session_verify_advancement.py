"""Regression: the boot Enter-drop (sac-tui-enter-drop-on-boot).

On every agent (re)start the boot ``startup_prompt`` was pasted into the
Claude-Code TUI but never submitted: the submit-Enter fired while Claude
was still BUSY / initializing (spinner ``Photosynthesizing…`` / ``Working…``
/ ``Ruminating…`` up, MCP mid-reconnect), and the Ink TUI silently drops
Enter in that window. The OLD ``_verify_submitted`` resent Enter up to 8x
back-to-back — but every one of those resends landed INSIDE the same busy
window, so all 8 dropped and the agent sat idle with its mission pasted
but unsent.

The fix (:func:`verify_submit_by_advancement`) replaces the blind resend
with **wait-for-idle + verify-by-advancement**:

  * never send Enter while the pane shows a spinner (gate on
    :func:`liveness_probe.pane_is_busy`, the fleet's SSOT busy detector);
  * after sending, verify SUBMISSION by buffer advancement — the
    ``❯ <text>`` compose buffer cleared (``prompts.detect`` no longer
    ``compose-pending-unsent``);
  * bounded attempts, then a fail-loud error with the pane tail.

These tests drive the pure function with REAL fake callables (a scripted
``capture_fn`` + a recording ``send_keys_fn``) — no MagicMock, no
monkeypatch-as-fixture-param (PA-306). STX-TQ002 AAA each on its own line,
STX-TQ007 one assertion per test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scitex_agent_container.runtimes.tui_session import (
    _compose_pending_live,
    verify_submit_by_advancement,
)

# ── Realistic pane snapshots ────────────────────────────────────────────────
# Distilled from live captures (ywata-note-win, 2026-06-20/23). Only the
# load-bearing glyphs matter to the detectors:
#   * busy        -> contains a DEFAULT_BUSY_MARKER ("Working…" / "esc to
#                    interrupt"); pane_is_busy() True.
#   * pending     -> "❯\xa0<text>" (NBSP gap, as Claude's Ink TUI renders a
#                    paste); prompts.detect() == "compose-pending-unsent".
#   * cleared     -> empty "❯ " prompt + "bypass permissions" status bar;
#                    NOT pending, and is_ready() True.

# Busy AND the paste already sitting in the compose buffer: a submit here
# would be dropped, so the loop must WAIT (not send Enter).
_BUSY_WITH_PENDING = (
    "❯\xa0go work\n  ✻ Photosynthesizing… (Working…  esc to interrupt)\n"
)
# Idle, paste still pending: this is the only state where Enter should fire.
_IDLE_WITH_PENDING = (
    "❯\xa0go work\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
# Submitted: the compose line cleared, agent is now at a fresh idle prompt.
_CLEARED = (
    "● Working on it.\n"
    "❯ \n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
# Never anything pending (e.g. instant submit / nothing to force).
_EMPTY_PROMPT = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"

# Submitted, but a prior "❯ …" line lingers in SCROLLBACK (e.g. a "❯ 1"
# echo or the previous turn's rendered prompt). The LIVE box (bottom-most
# ❯) is empty, so this is NOT pending. The OLD whole-pane detector matched
# the scrollback ❯ and false-reported "still pending" forever — the
# sac-tui-enter-drop-on-boot live regression (scitex-dev, 2026-06-24).
_CLEARED_WITH_SCROLLBACK_PROMPT = (
    "❯\xa0go work\n"
    "● Working on it.\n"
    "❯ \n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)


# ── Fakes (real callables, not mocks) ───────────────────────────────────────


@dataclass
class _ScriptedPane:
    """A ``capture_fn`` that returns a scripted sequence of pane snapshots.

    Each call returns the next snapshot; once the script is exhausted it
    keeps returning the LAST one (a steady terminal state). Records the
    number of captures so a test can correlate captures with sends.
    """

    snapshots: list[str]
    captures: int = 0

    def __call__(self, _name: str) -> str:
        idx = min(self.captures, len(self.snapshots) - 1)
        self.captures += 1
        return self.snapshots[idx]


@dataclass
class _RecordingSend:
    """A ``send_keys_fn`` that records every key sent."""

    keys: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.keys.append(key)


class _PendingUntilEnter:
    """State-machine ``capture_fn``: stays at ``pending`` until ``release``
    Enters have been recorded by the paired sender, then reports ``cleared``.

    Models the real TUI: the buffer is pasted-but-unsent until an Enter
    actually LANDS; the ``release`` knob lets a test simulate the Ink TUI
    eating the first N Enters (drop) before one finally submits.
    """

    def __init__(
        self,
        sender: _RecordingSend,
        *,
        pending: str = _IDLE_WITH_PENDING,
        cleared: str = _CLEARED,
        release_after: int = 1,
    ) -> None:
        self._sender = sender
        self._pending = pending
        self._cleared = cleared
        self._release_after = release_after

    def __call__(self, _name: str) -> str:
        enters = self._sender.keys.count("Enter")
        return self._cleared if enters >= self._release_after else self._pending


class _BusyThenIdleThenCleared:
    """State-machine ``capture_fn``: BUSY (with paste pending) for the first
    ``busy_captures`` reads, then idle-with-pending, then — once one Enter has
    landed — cleared.

    Mirrors a real (re)start: the spinner is up while Claude initializes
    (paste already on the input line), so the loop must WAIT; the moment the
    spinner clears, the single Enter submits.
    """

    def __init__(self, sender: _RecordingSend, *, busy_captures: int = 1) -> None:
        self._sender = sender
        self._busy_captures = busy_captures
        self._reads = 0

    def __call__(self, _name: str) -> str:
        if self._sender.keys.count("Enter") >= 1:
            return _CLEARED
        self._reads += 1
        if self._reads <= self._busy_captures:
            return _BUSY_WITH_PENDING
        return _IDLE_WITH_PENDING


def _no_sleep(_s: float) -> None:
    return None


class _FakeClock:
    """Monotonic clock that advances by a fixed step on every read.

    Lets idle/verify deadlines expire deterministically without real
    waits — every ``time_fn()`` call ticks forward, so a bounded loop
    always terminates.
    """

    def __init__(self, step: float = 0.1) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


# ── (a) waits while busy: no Enter sent during a spinner ─────────────────────


def test_does_not_send_enter_while_pane_is_busy() -> None:
    # Arrange — pane is busy (spinner up) with the paste pending the whole
    # time; the idle window expires without ever going idle.
    capture = _ScriptedPane([_BUSY_WITH_PENDING])
    sender = _RecordingSend()
    # Act — bounded so the busy idle-wait elapses across attempts.
    verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=2,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=0.3,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — not a single Enter was sent into the busy window.
    assert sender.keys == []


# ── (b) sends Enter once idle + pending ──────────────────────────────────────


def test_sends_enter_once_pane_is_idle_with_pending_buffer() -> None:
    # Arrange — BUSY (paste pending) for the first frames, then idle; the
    # single Enter that fires once idle clears the buffer.
    sender = _RecordingSend()
    capture = _BusyThenIdleThenCleared(sender, busy_captures=2)
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=3,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — exactly one Enter was sent (once idle), and it submitted.
    assert (ok, sender.keys) == (True, ["Enter"])


# ── (c) detects buffer advancement = submitted = stop ────────────────────────


def test_returns_true_and_stops_when_buffer_advances_after_enter() -> None:
    # Arrange — idle+pending immediately; a single Enter clears the buffer.
    sender = _RecordingSend()
    capture = _PendingUntilEnter(sender, release_after=1)
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=8,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — submission verified by advancement; loop stopped after 1 Enter.
    assert (ok, len(sender.keys)) == (True, 1)


# ── (d) resends when not advanced and now idle ───────────────────────────────


def test_resends_enter_when_first_is_dropped_then_idle_again() -> None:
    # Arrange — the Ink TUI eats the FIRST Enter (buffer stays pending);
    # the second Enter lands. Pane is idle throughout (no spinner), so the
    # difference from the busy case is purely the drop-then-resend.
    sender = _RecordingSend()
    capture = _PendingUntilEnter(sender, release_after=2)
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=8,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — it resent until the buffer advanced (2 Enters), then stopped.
    assert (ok, sender.keys) == (True, ["Enter", "Enter"])


# ── (e) bounded attempts then fail-loud ──────────────────────────────────────


def test_fails_loud_and_returns_false_when_buffer_never_advances() -> None:
    # Arrange — idle+pending but Enter NEVER clears the buffer (release set
    # impossibly high): the Ink TUI drops every send.
    sender = _RecordingSend()
    capture = _PendingUntilEnter(sender, release_after=999)
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=4,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — bounded to max_resends attempts, then a fail-loud False.
    assert (ok, sender.keys.count("Enter")) == (False, 4)


def test_bounded_attempts_does_not_exceed_max_resends() -> None:
    # Arrange — same never-advancing pane, a different cap.
    sender = _RecordingSend()
    capture = _PendingUntilEnter(sender, release_after=999)
    # Act
    verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=2,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — never sends more Enters than the bound allows.
    assert sender.keys.count("Enter") == 2


# ── nothing-to-submit short-circuit ─────────────────────────────────────────


def test_returns_true_without_sending_when_nothing_pending() -> None:
    # Arrange — the paste never renders as a pending buffer (submitted
    # instantly, or there was nothing to force).
    capture = _ScriptedPane([_EMPTY_PROMPT])
    sender = _RecordingSend()
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=8,
        poll_s=0.0,
        appear_timeout_s=0.3,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — nothing to force: True, and no Enter sent.
    assert (ok, sender.keys) == (True, [])


# ── bails on advancement during the idle-wait (operator submitted) ──────────


def test_returns_true_when_buffer_advances_before_any_enter() -> None:
    # Arrange — pending appears, then clears on its own (e.g. the operator
    # pressed Enter, or a prior turn submitted) BEFORE the loop sends Enter.
    snapshots = [_IDLE_WITH_PENDING, _CLEARED]
    capture = _ScriptedPane(snapshots)
    sender = _RecordingSend()
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=8,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — detected advancement, returned True, sent no stray Enter.
    assert (ok, sender.keys) == (True, [])


# ── (h) regression: a scrollback "❯ …" must not mask a cleared live box ──────


def test_compose_pending_live_ignores_scrollback_prompt() -> None:
    # Arrange — live box empty, but a "❯ …" line remains in scrollback.
    pane = _CLEARED_WITH_SCROLLBACK_PROMPT
    # Act
    pending = _compose_pending_live(pane)
    # Assert — the live (bottom-most) box is empty, so NOT pending.
    assert pending is False


def test_compose_pending_live_flags_live_unsent_text() -> None:
    # Arrange — the live (bottom-most) ❯ holds unsent text.
    pane = _IDLE_WITH_PENDING
    # Act
    pending = _compose_pending_live(pane)
    # Assert
    assert pending is True


def test_advancement_detected_despite_scrollback_prompt() -> None:
    # Arrange — after the Enter lands the live box clears, but a prior
    # "❯ …" line lingers in SCROLLBACK. The old whole-pane detector kept
    # reading "pending" forever and false-failed; the live-box check sees
    # the submission (the sac-tui-enter-drop-on-boot live fix, 2026-06-24).
    sender = _RecordingSend()
    capture = _PendingUntilEnter(
        sender, cleared=_CLEARED_WITH_SCROLLBACK_PROMPT, release_after=1
    )
    # Act
    ok = verify_submit_by_advancement(
        "tui-x",
        capture_fn=capture,
        send_keys_fn=sender,
        max_resends=4,
        poll_s=0.0,
        appear_timeout_s=5.0,
        idle_wait_s=5.0,
        sleep_fn=_no_sleep,
        time_fn=_FakeClock(step=0.1),
    )
    # Assert — submission recognized despite the scrollback ❯.
    assert ok is True
