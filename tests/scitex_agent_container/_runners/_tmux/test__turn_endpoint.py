"""Day-2 (B): A2A → tmux bridge tests.

The bridge is exercised against a memory-backed fake ``TmuxDriver`` so
the tests never need ``tmux`` installed. Each test focuses on one
contract:

* round-trip turn-text → exact send-keys argv sequence
* pane delta returned on ready-marker detection
* timeout raises with the partial pane delta on the exception
* timeout DOES NOT call ``kill-session`` (the multiplexer survives)

Test style (project standards — STX-TQ002 / STX-TQ007):
* Each test carries explicit ``# Arrange`` / ``# Act`` / ``# Assert``
  markers on their own lines in order.
* One assertion per test. Multi-assert observations are split across
  one-assert-per-test functions so the precise failure surfaces.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._tmux._turn_endpoint import (
    TurnResult,
    TurnTimeoutError,
    _pane_delta,
    inject_turn,
)

# is_ready (from .prompts) returns True iff "bypass permissions" in
# content AND "Enter to confirm" not in content. The fakes below craft
# pane content with that marker so we can drive the ready transition.

READY_TAIL = "\n❯                                  | bypass permissions  off\n"
NOT_READY_TAIL = "\nWorking…  esc to interrupt\n"


# ---------------------------------------------------------------------------
# Fake TmuxDriver (in-test real implementation, not a mock)
# ---------------------------------------------------------------------------


class FakeTmux:
    """In-memory tmux pane.

    The pane content evolves through a script of states ``pane_states``
    supplied at construction. Each ``capture_pane`` call advances the
    cursor; sticking on the last state once exhausted lets a test pin
    "the pane stays in this state forever".

    All ``send_keys`` calls are recorded as a list of
    ``(session, *keys)`` tuples so the round-trip-argv test can assert
    the exact sequence.

    ``kill_calls`` records ``kill_session`` calls — used by the "did
    NOT kill on timeout" test. The bridge never calls kill itself,
    so this should stay empty.
    """

    def __init__(self, pane_states: list[str]):
        if not pane_states:
            pane_states = [""]
        self._states = list(pane_states)
        self._cursor = 0
        self.send_calls: list[tuple] = []
        self.capture_calls = 0
        self.kill_calls: list[str] = []

    # ---- TmuxDriver Protocol --------------------------------------------

    def send_keys(self, session: str, *keys: str) -> None:
        self.send_calls.append((session, *keys))

    def capture_pane(self, session: str) -> str:
        self.capture_calls += 1
        state = self._states[self._cursor]
        if self._cursor < len(self._states) - 1:
            self._cursor += 1
        return state

    def session_exists(self, session: str) -> bool:
        return True

    # ---- Test-only helper -----------------------------------------------

    def kill_session(self, session: str) -> None:
        self.kill_calls.append(session)


def _fake_sleep_clock():
    """Return (sleep_fn, monotonic_fn) with virtual time so tests are fast."""
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def monotonic() -> float:
        return now[0]

    return sleep, monotonic


# ---------------------------------------------------------------------------
# inject_turn — send-keys argv sequence
# ---------------------------------------------------------------------------


def _ready_tmux_after_one_turn() -> "FakeTmux":
    """Helper: a FakeTmux that returns baseline first, then a ready pane."""
    pane_baseline = "❯  \n"
    pane_ready = pane_baseline + READY_TAIL
    return FakeTmux([pane_baseline, pane_ready])


def test_inject_turn_send_calls_first_entry_is_text():
    """B.1: bridge issues the prompt text as its first send-keys call."""
    # Arrange
    fake = _ready_tmux_after_one_turn()
    sleep, mono = _fake_sleep_clock()
    # Act
    inject_turn(
        fake,
        "sac-claude",
        "hello world",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert fake.send_calls[0] == ("sac-claude", "hello world")


def test_inject_turn_send_calls_second_entry_is_enter():
    """B.1: bridge issues Enter as a SEPARATE send-keys call (trailing
    ``\\r`` in the text would be dropped by the TUI on a re-render)."""
    # Arrange
    fake = _ready_tmux_after_one_turn()
    sleep, mono = _fake_sleep_clock()
    # Act
    inject_turn(
        fake,
        "sac-claude",
        "hello world",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert fake.send_calls[1] == ("sac-claude", "Enter")


def test_inject_turn_returns_turn_result_instance():
    # Arrange
    fake = _ready_tmux_after_one_turn()
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-claude",
        "hello world",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert isinstance(result, TurnResult)


def test_inject_turn_result_not_timed_out_on_ready():
    # Arrange
    fake = _ready_tmux_after_one_turn()
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-claude",
        "hello world",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert not result.timed_out


# ---------------------------------------------------------------------------
# inject_turn — pane delta semantics
# ---------------------------------------------------------------------------


def _delta_scenario_fake() -> "FakeTmux":
    """Helper: baseline then a ready pane containing 'assistant reply here'."""
    baseline = "previous turn output\n❯  \n"
    after_ready = baseline + "assistant reply here\n" + READY_TAIL
    return FakeTmux([baseline, after_ready])


def test_inject_turn_delta_contains_post_injection_reply():
    """B contract: returned text contains the post-injection suffix."""
    # Arrange
    fake = _delta_scenario_fake()
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-x",
        "what is 2+2",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert "assistant reply here" in result.text


def test_inject_turn_delta_does_not_contain_pre_injection_baseline():
    """The delta is the suffix after baseline, not the full pane."""
    # Arrange
    fake = _delta_scenario_fake()
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-x",
        "what is 2+2",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert "previous turn output" not in result.text


def test_inject_turn_poll_count_one_when_ready_on_first_capture():
    # Arrange
    fake = _delta_scenario_fake()
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-x",
        "what is 2+2",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert result.poll_count == 1


# ---------------------------------------------------------------------------
# inject_turn — timeout semantics
# ---------------------------------------------------------------------------


def _busy_forever_fake() -> "FakeTmux":
    """Helper: pane stays 'busy' (no ready marker) forever."""
    baseline = "❯  \n"
    busy = baseline + NOT_READY_TAIL
    return FakeTmux([baseline, busy])


def _trigger_timeout(fake: "FakeTmux", session: str = "sac-y", text: str = "hung turn"):
    """Helper: call inject_turn against the busy-forever fake and return
    the caught :class:`TurnTimeoutError`. Avoids ``with pytest.raises``
    plus a post-condition ``assert`` (STX-TQ007: that counts as 2)."""
    sleep, mono = _fake_sleep_clock()
    try:
        inject_turn(
            fake,
            session,
            text,
            timeout_s=3.0,
            poll_interval_s=1.0,
            sleep_fn=sleep,
            monotonic_fn=mono,
        )
    except TurnTimeoutError as exc:
        return exc
    return None  # caller asserts not-None to fail loudly


def test_inject_turn_raises_turn_timeout_when_ready_never_appears():
    """B contract: bridge raises after N polls without a ready marker."""
    # Arrange
    fake = _busy_forever_fake()
    # Act
    err = _trigger_timeout(fake)
    # Assert
    assert isinstance(err, TurnTimeoutError)


def test_inject_turn_timeout_carries_session_on_exception():
    # Arrange
    fake = _busy_forever_fake()
    # Act
    err = _trigger_timeout(fake, session="sac-y")
    # Assert
    assert err.session == "sac-y"


def test_inject_turn_timeout_carries_timed_out_flag_on_exception():
    # Arrange
    fake = _busy_forever_fake()
    # Act
    err = _trigger_timeout(fake)
    # Assert
    assert err.result.timed_out is True


def test_inject_turn_timeout_carries_partial_pane_delta_on_exception():
    """Partial pane delta is carried on the exception so the HTTP layer
    can surface it in the 504 body. Accept either the busy marker or
    an empty partial delta."""
    # Arrange
    fake = _busy_forever_fake()
    # Act
    err = _trigger_timeout(fake)
    # Assert
    assert "Working" in err.result.text or err.result.text == ""


def test_inject_turn_timeout_does_not_kill_session():
    """B.4 contract: bridge MUST leave the tmux session alive on timeout.

    A turn timeout might just mean a long tool call; killing the
    multiplexer would wipe the agent's whole TUI state.
    """
    # Arrange
    fake = _busy_forever_fake()
    # Act
    _trigger_timeout(fake, session="sac-z", text="no-reply")
    # Assert
    assert fake.kill_calls == []


def test_inject_turn_timeout_does_not_inject_cancel_keys():
    """The bridge does not even probe session_exists for kill-decisions —
    it simply re-raises. The send_keys log must show no follow-up
    cancellation key (e.g., C-c) being injected either."""
    # Arrange
    fake = _busy_forever_fake()
    # Act
    _trigger_timeout(fake, session="sac-z", text="no-reply")
    # Assert
    assert ("sac-z", "C-c") not in fake.send_calls


# ---------------------------------------------------------------------------
# _pane_delta helper
# ---------------------------------------------------------------------------


def test_pane_delta_returns_tail_when_baseline_is_prefix():
    """``_pane_delta`` returns the suffix when the baseline is a prefix."""
    # Arrange
    baseline = "abc\n"
    current = "abc\nDEF\n"
    # Act
    delta = _pane_delta(baseline, current)
    # Assert
    assert delta == "DEF\n"


def test_pane_delta_returns_full_current_when_baseline_not_prefix():
    """When the pane has scrolled out of view, return ``current`` verbatim."""
    # Arrange
    baseline = "abc\n"
    current = "completely different\n"
    # Act
    delta = _pane_delta(baseline, current)
    # Assert
    assert delta == "completely different\n"


# ---------------------------------------------------------------------------
# inject_turn — polling loop semantics
# ---------------------------------------------------------------------------


def _multi_poll_states() -> list[str]:
    baseline = "❯  \n"
    return [
        baseline,
        baseline + NOT_READY_TAIL,
        baseline + NOT_READY_TAIL,
        baseline + "partial reply\n" + NOT_READY_TAIL,
        baseline + "full reply\n" + READY_TAIL,
    ]


def test_inject_turn_does_not_time_out_when_ready_eventually_appears():
    # Arrange
    fake = FakeTmux(_multi_poll_states())
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-poll",
        "multi-poll turn",
        timeout_s=100.0,
        poll_interval_s=1.0,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert not result.timed_out


def test_inject_turn_text_contains_full_reply_after_polling():
    # Arrange
    fake = FakeTmux(_multi_poll_states())
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-poll",
        "multi-poll turn",
        timeout_s=100.0,
        poll_interval_s=1.0,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert "full reply" in result.text


def test_inject_turn_poll_count_at_least_two_when_multi_poll():
    # Arrange
    fake = FakeTmux(_multi_poll_states())
    sleep, mono = _fake_sleep_clock()
    # Act
    result = inject_turn(
        fake,
        "sac-poll",
        "multi-poll turn",
        timeout_s=100.0,
        poll_interval_s=1.0,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )
    # Assert
    assert result.poll_count >= 2


def test_inject_turn_session_never_killed_across_repeated_failures():
    """Even after many failed turns, the bridge never escalates to kill."""
    # Arrange
    fake = _busy_forever_fake()
    # Act
    for _ in range(3):
        _trigger_timeout(fake, session="sac-survives", text="x")
    # Assert
    assert fake.kill_calls == []
