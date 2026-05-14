"""Tests for the functional-liveness probe state machine.

The module is pure observation: it classifies pane captures into
ProbeState without sending any keystrokes. These tests verify the
classifier in isolation and the polling loop using injected clocks
so no real sleeping or subprocess work happens.
"""

from __future__ import annotations

from typing import Iterable

import pytest

from scitex_agent_container._lifecycle.liveness_probe import (
    DEFAULT_BUSY_MARKERS,
    ProbeState,
    classify_probe,
    generate_nonce,
    pane_has_nonce_echo,
    pane_is_busy,
    wait_for_nonce_echo,
)

# ── Nonce generation ─────────────────────────────────────────────────────────


class TestGenerateNonce:
    def test_default_length_is_8_hex_chars(self):
        n = generate_nonce()
        assert len(n) == 8
        assert all(c in "0123456789abcdef" for c in n)

    def test_custom_byte_count(self):
        n = generate_nonce(n_bytes=6)
        assert len(n) == 12

    def test_nonces_are_unique_in_practice(self):
        """Collision probability over 1000 8-hex samples is negligible."""
        samples = {generate_nonce() for _ in range(1000)}
        assert len(samples) > 990


# ── Echo detection ───────────────────────────────────────────────────────────


class TestPaneHasNonceEcho:
    def test_no_pane_text(self):
        assert pane_has_nonce_echo("", "abc123") is False

    def test_no_nonce(self):
        assert pane_has_nonce_echo("some output", "") is False

    def test_prompt_only_is_not_echo(self):
        """The user-sent prompt line alone contains the nonce once.
        That must not count as alive — we need the echo."""
        pane = "> Repeat abc123\n"
        assert pane_has_nonce_echo(pane, "abc123") is False

    def test_prompt_plus_echo_counts_as_alive(self):
        pane = "> Repeat abc123\nI will echo it:\nabc123\n"
        assert pane_has_nonce_echo(pane, "abc123") is True

    def test_echo_in_sentence(self):
        """Any second occurrence counts — the wording doesn't matter."""
        pane = "> Repeat abc123\nSure — the code is abc123 as requested.\n"
        assert pane_has_nonce_echo(pane, "abc123") is True

    def test_min_occurrences_tunable(self):
        """Operators on soft-wrapping terminals may bump the threshold."""
        pane = "> Repeat abc123\nReply: abc123\n"
        assert pane_has_nonce_echo(pane, "abc123", min_occurrences=2) is True
        assert pane_has_nonce_echo(pane, "abc123", min_occurrences=3) is False


# ── Busy detection ───────────────────────────────────────────────────────────


class TestPaneIsBusy:
    def test_empty_pane_not_busy(self):
        assert pane_is_busy("") is False

    def test_working_marker(self):
        assert pane_is_busy("... Working\u2026 ...") is True

    def test_ruminating_marker(self):
        assert pane_is_busy("status: Ruminating\u2026") is True

    def test_esc_to_interrupt_marker(self):
        """Claude Code prints 'esc to interrupt' while generating."""
        assert pane_is_busy("Processing... (esc to interrupt)") is True

    def test_quiet_pane_not_busy(self):
        assert pane_is_busy("> \nbypass permissions active\n") is False

    def test_only_tail_is_classified(self):
        """A historical 'Working…' that scrolled away must not count."""
        old = "Working\u2026 earlier\n"
        new_tail = "\nquiet now\n> \n"
        huge_noise = "x" * 5000
        pane = old + huge_noise + new_tail
        # 2000-char tail window strips the old marker.
        assert pane_is_busy(pane, tail_chars=2000) is False

    def test_custom_markers_override_default(self):
        """Callers can pass a tighter / looser marker list."""
        pane = "Hmm… mulling"
        assert pane_is_busy(pane) is False  # "mulling" not a default marker
        assert pane_is_busy(pane, markers=("mulling",)) is True


# ── Single-capture classifier ────────────────────────────────────────────────


class TestClassifyProbe:
    def test_echo_beats_everything_returns_alive(self):
        """ALIVE wins even at timeout and even if the pane looks busy —
        the echo already proved the agent is responsive."""
        pane = "> Repeat abc\nabc\nWorking\u2026\n"
        assert classify_probe(pane, "abc", is_timeout=True) is ProbeState.ALIVE

    def test_no_echo_no_timeout_is_pending(self):
        pane = "> Repeat abc\n"
        assert classify_probe(pane, "abc", is_timeout=False) is ProbeState.PENDING

    def test_no_echo_timeout_and_busy_marker_is_busy(self):
        """Timeout + no echo + pane actively working -> BUSY.
        Caller should defer re-probing rather than declare silent."""
        pane = "> Repeat abc\n... Working\u2026 ...\n"
        assert classify_probe(pane, "abc", is_timeout=True) is ProbeState.BUSY

    def test_no_echo_timeout_quiet_is_silent(self):
        """Timeout + no echo + quiet pane -> SILENT (agent looks frozen)."""
        pane = "> Repeat abc\n(nothing else)\n"
        assert classify_probe(pane, "abc", is_timeout=True) is ProbeState.SILENT


# ── Polling loop ─────────────────────────────────────────────────────────────


class _FakeClock:
    """Deterministic clock: ``sleep(d)`` advances the clock by ``d``.
    ``now()`` returns the current value without advancing."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.t += d


class _SeqCapture:
    """Return a pre-defined sequence of pane captures; stick on last."""

    def __init__(self, seq: Iterable[str]):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self, _pane_target: str) -> str:
        self.calls += 1
        if not self.seq:
            return ""
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


@pytest.fixture
def clock():
    return _FakeClock()


def _run(captures, *, timeout=10.0, poll_interval=2.0, nonce="dead1234"):
    clk = _FakeClock()
    cap = _SeqCapture(captures)
    return (
        wait_for_nonce_echo(
            agent_name="test-agent",
            pane_target="test-pane",
            nonce=nonce,
            poll_interval=poll_interval,
            timeout=timeout,
            capture_fn=cap,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        ),
        cap,
        clk,
    )


class TestWaitForNonceEcho:
    def test_first_capture_has_echo_returns_alive_zero_elapsed(self):
        prompt_with_echo = "> Repeat dead1234\nEcho: dead1234\n"
        (state, elapsed), cap, clk = _run([prompt_with_echo])
        assert state is ProbeState.ALIVE
        assert elapsed == 0.0
        assert cap.calls == 1

    def test_echo_appears_after_a_few_polls(self):
        """Typical path: first 2 captures show prompt only, 3rd shows echo."""
        prompt = "> Repeat dead1234\n"
        echoed = prompt + "dead1234\n"
        (state, elapsed), cap, _ = _run(
            [prompt, prompt, echoed],
            poll_interval=2.0,
        )
        assert state is ProbeState.ALIVE
        # Two sleeps between three captures, 2s each -> 4s elapsed.
        assert elapsed == 4.0
        assert cap.calls == 3

    def test_timeout_with_quiet_pane_is_silent(self):
        """Pane never changes, no busy marker -> SILENT at deadline."""
        prompt_only = "> Repeat dead1234\n"
        (state, elapsed), _, _ = _run([prompt_only], timeout=6.0, poll_interval=2.0)
        assert state is ProbeState.SILENT
        # Loop: t=0 poll, sleep 2 -> t=2, poll, sleep 2 -> t=4, poll, sleep 2
        # -> t=6 deadline, final poll classifies silent.
        assert elapsed >= 6.0

    def test_timeout_with_busy_marker_is_busy(self):
        """Pane shows 'Working…' throughout -> BUSY at deadline."""
        busy_pane = "> Repeat dead1234\nWorking\u2026\n"
        (state, _), _, _ = _run([busy_pane], timeout=4.0, poll_interval=2.0)
        assert state is ProbeState.BUSY

    def test_alive_short_circuits_before_timeout(self):
        """Don't keep polling after the echo arrives."""
        echoed = "> Repeat dead1234\ndead1234 — done\n"
        (state, _), cap, clk = _run([echoed], timeout=60.0, poll_interval=2.0)
        assert state is ProbeState.ALIVE
        assert cap.calls == 1  # exactly one capture before returning
        assert clk.t == 0.0  # no sleep fired

    def test_default_busy_markers_cover_common_cases(self):
        """Spot-check a couple of the markers the default list should cover."""
        assert "Working\u2026" in DEFAULT_BUSY_MARKERS
        assert "Ruminating\u2026" in DEFAULT_BUSY_MARKERS
        assert "esc to interrupt" in DEFAULT_BUSY_MARKERS

    def test_capture_fn_returning_none_treated_as_empty(self):
        class NoneCapture:
            calls = 0

            def __call__(self, _t):
                self.calls += 1
                return None  # type: ignore[return-value]

        clk = _FakeClock()
        cap = NoneCapture()
        state, _ = wait_for_nonce_echo(
            agent_name="t",
            pane_target="p",
            nonce="dead1234",
            poll_interval=1.0,
            timeout=2.0,
            capture_fn=cap,  # type: ignore[arg-type]
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        # Empty pane -> not busy -> SILENT at deadline.
        assert state is ProbeState.SILENT

    def test_capture_fn_raising_is_caught_and_treated_as_empty(self):
        """A transient tmux failure should not abort the loop."""

        calls = {"n": 0}

        def flaky(_t):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("tmux went away briefly")
            return "> Repeat dead1234\ndead1234\n"

        clk = _FakeClock()
        state, _ = wait_for_nonce_echo(
            agent_name="t",
            pane_target="p",
            nonce="dead1234",
            poll_interval=1.0,
            timeout=10.0,
            capture_fn=flaky,
            time_fn=clk.now,
            sleep_fn=clk.sleep,
        )
        assert state is ProbeState.ALIVE
        assert calls["n"] == 2
