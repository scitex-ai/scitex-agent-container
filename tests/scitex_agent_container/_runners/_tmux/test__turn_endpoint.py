"""Day-2 (B): A2A → tmux bridge tests.

The bridge is exercised against a memory-backed fake ``TmuxDriver`` so
the tests never need ``tmux`` installed. Each test focuses on one
contract:

* round-trip turn-text → exact send-keys argv sequence
* pane delta returned on ready-marker detection
* timeout raises with the partial pane delta on the exception
* timeout DOES NOT call ``kill-session`` (the multiplexer survives)
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
# Fake TmuxDriver
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
# Tests
# ---------------------------------------------------------------------------


def test_inject_turn_sends_text_then_enter_in_separate_calls():
    """B.1 contract: bridge issues exactly ``text`` then ``Enter``.

    Mirrors the salvaged ``TmuxManager.send_text_and_submit`` insight:
    the two keystrokes must be SEPARATE ``send-keys`` calls (a
    trailing ``\\r`` would be sent as raw input and the TUI sometimes
    drops it during a re-render).
    """
    pane_baseline = "❯  \n"
    pane_ready = pane_baseline + READY_TAIL
    fake = FakeTmux([pane_baseline, pane_ready])
    sleep, mono = _fake_sleep_clock()

    result = inject_turn(
        fake,
        "sac-claude",
        "hello world",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )

    # Exactly the inject keystrokes: text first, then Enter.
    assert fake.send_calls == [
        ("sac-claude", "hello world"),
        ("sac-claude", "Enter"),
    ]
    assert isinstance(result, TurnResult)
    assert not result.timed_out


def test_inject_turn_returns_pane_delta_on_ready():
    """B contract: bridge returns the suffix that appeared post-injection."""
    baseline = "previous turn output\n❯  \n"
    after_ready = baseline + "assistant reply here\n" + READY_TAIL
    fake = FakeTmux([baseline, after_ready])
    sleep, mono = _fake_sleep_clock()

    result = inject_turn(
        fake,
        "sac-x",
        "what is 2+2",
        timeout_s=10.0,
        poll_interval_s=0.5,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )

    assert not result.timed_out
    assert "assistant reply here" in result.text
    # The delta is the suffix after baseline, not the full pane.
    assert "previous turn output" not in result.text
    assert result.poll_count == 1


def test_inject_turn_raises_on_timeout_no_ready_marker():
    """B contract: bridge raises after N polls without a ready marker."""
    baseline = "❯  \n"
    busy = baseline + NOT_READY_TAIL
    # Pane stays "busy" forever — ready marker never appears.
    fake = FakeTmux([baseline, busy])
    sleep, mono = _fake_sleep_clock()

    with pytest.raises(TurnTimeoutError) as excinfo:
        inject_turn(
            fake,
            "sac-y",
            "hung turn",
            timeout_s=3.0,
            poll_interval_s=1.0,
            sleep_fn=sleep,
            monotonic_fn=mono,
        )

    err = excinfo.value
    assert err.session == "sac-y"
    assert err.result.timed_out is True
    # Partial pane delta is carried on the exception so the HTTP layer
    # can surface it in the 504 body.
    assert "Working" in err.result.text or err.result.text == ""


def test_inject_turn_does_not_kill_session_on_timeout():
    """B.4 contract: bridge MUST leave the tmux session alive on timeout.

    A turn timeout might just mean a long tool call; killing the
    multiplexer would wipe the agent's whole TUI state.
    """
    baseline = "❯  \n"
    busy = baseline + NOT_READY_TAIL
    fake = FakeTmux([baseline, busy])
    sleep, mono = _fake_sleep_clock()

    with pytest.raises(TurnTimeoutError):
        inject_turn(
            fake,
            "sac-z",
            "no-reply",
            timeout_s=2.0,
            poll_interval_s=0.5,
            sleep_fn=sleep,
            monotonic_fn=mono,
        )

    # Bridge MUST NOT have asked the driver to kill the session.
    assert fake.kill_calls == []
    # Bridge does not even probe session_exists for kill-decisions —
    # it simply re-raises. The send_keys log must show no follow-up
    # cancellation key (e.g., C-c) being injected either.
    cancel_seq = ("sac-z", "C-c")
    assert cancel_seq not in fake.send_calls


def test_pane_delta_returns_tail_when_baseline_is_prefix():
    """``_pane_delta`` returns the suffix when the baseline is a prefix."""
    assert _pane_delta("abc\n", "abc\nDEF\n") == "DEF\n"


def test_pane_delta_returns_full_current_when_baseline_not_prefix():
    """When the pane has scrolled out of view, return ``current`` verbatim."""
    assert _pane_delta("abc\n", "completely different\n") == ("completely different\n")


def test_inject_turn_keeps_polling_until_ready_marker_appears():
    """The poll loop spins through busy states before finding ready."""
    baseline = "❯  \n"
    states = [
        baseline,
        baseline + NOT_READY_TAIL,
        baseline + NOT_READY_TAIL,
        baseline + "partial reply\n" + NOT_READY_TAIL,
        baseline + "full reply\n" + READY_TAIL,
    ]
    fake = FakeTmux(states)
    sleep, mono = _fake_sleep_clock()

    result = inject_turn(
        fake,
        "sac-poll",
        "multi-poll turn",
        timeout_s=100.0,
        poll_interval_s=1.0,
        sleep_fn=sleep,
        monotonic_fn=mono,
    )

    assert not result.timed_out
    assert "full reply" in result.text
    assert result.poll_count >= 2


def test_inject_turn_session_survives_on_repeated_failures():
    """Even after many failed turns, the bridge never escalates to kill."""
    baseline = "❯  \n"
    busy = baseline + NOT_READY_TAIL
    fake = FakeTmux([baseline, busy])
    sleep, mono = _fake_sleep_clock()

    for _ in range(3):
        with pytest.raises(TurnTimeoutError):
            inject_turn(
                fake,
                "sac-survives",
                "x",
                timeout_s=2.0,
                poll_interval_s=0.5,
                sleep_fn=sleep,
                monotonic_fn=mono,
            )

    assert fake.kill_calls == []
