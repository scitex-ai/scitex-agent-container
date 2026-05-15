"""Tests for the functional-liveness probe state machine.

The module is pure observation: it classifies pane captures into
ProbeState without sending any keystrokes. These tests verify the
classifier in isolation and the polling loop using injected clocks
so no real sleeping or subprocess work happens.

TQ cleanup (PA-306 follow-up): every test now has the AAA marker
comments, a `<subject>_<condition>_<expected>` name with at least
three word-tokens after ``test_``, and exactly one assertion. Where
multiple inputs exercise the same behaviour, ``pytest.parametrize``
collapses the cases. No mocks / monkeypatch were introduced — the
existing in-test fakes (``_FakeClock`` / ``_SeqCapture``) are real
deterministic callables injected via the module's documented
``capture_fn`` / ``time_fn`` / ``sleep_fn`` seams.
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


def test_generate_nonce_default_returns_eight_char_string():
    # Arrange
    # (no fixture; generate_nonce is a pure function)
    # Act
    nonce = generate_nonce()
    # Assert
    assert len(nonce) == 8


def test_generate_nonce_default_returns_lowercase_hex_chars_only():
    # Arrange
    hex_chars = set("0123456789abcdef")
    # Act
    nonce = generate_nonce()
    # Assert
    assert set(nonce) <= hex_chars


@pytest.mark.parametrize(
    "n_bytes, expected_len",
    [(1, 2), (4, 8), (6, 12), (8, 16)],
)
def test_generate_nonce_with_custom_byte_count_returns_double_length_hex(
    n_bytes, expected_len
):
    # Arrange
    # (parameterised over byte counts -> expected hex length 2*n_bytes)
    # Act
    nonce = generate_nonce(n_bytes=n_bytes)
    # Assert
    assert len(nonce) == expected_len


def test_generate_nonce_default_produces_near_unique_samples_in_practice():
    """Collision probability over 1000 8-hex samples is negligible."""
    # Arrange
    sample_count = 1_000
    # Act
    samples = {generate_nonce() for _ in range(sample_count)}
    # Assert
    assert len(samples) > 990


# ── Echo detection ───────────────────────────────────────────────────────────


def test_pane_has_nonce_echo_with_empty_pane_returns_false():
    # Arrange
    pane = ""
    # Act
    result = pane_has_nonce_echo(pane, "abc123")
    # Assert
    assert result is False


def test_pane_has_nonce_echo_with_empty_nonce_returns_false():
    # Arrange
    pane = "some output"
    # Act
    result = pane_has_nonce_echo(pane, "")
    # Assert
    assert result is False


def test_pane_has_nonce_echo_with_prompt_only_returns_false():
    """The user-sent prompt line alone contains the nonce once.
    That must not count as alive — we need the echo."""
    # Arrange
    pane = "> Repeat abc123\n"
    # Act
    result = pane_has_nonce_echo(pane, "abc123")
    # Assert
    assert result is False


def test_pane_has_nonce_echo_with_prompt_plus_echo_returns_true():
    # Arrange
    pane = "> Repeat abc123\nI will echo it:\nabc123\n"
    # Act
    result = pane_has_nonce_echo(pane, "abc123")
    # Assert
    assert result is True


def test_pane_has_nonce_echo_with_echo_inside_sentence_returns_true():
    """Any second occurrence counts — the wording doesn't matter."""
    # Arrange
    pane = "> Repeat abc123\nSure — the code is abc123 as requested.\n"
    # Act
    result = pane_has_nonce_echo(pane, "abc123")
    # Assert
    assert result is True


def test_pane_has_nonce_echo_with_min_occurrences_two_and_two_hits_returns_true():
    # Arrange
    pane = "> Repeat abc123\nReply: abc123\n"
    # Act
    result = pane_has_nonce_echo(pane, "abc123", min_occurrences=2)
    # Assert
    assert result is True


def test_pane_has_nonce_echo_with_min_occurrences_three_and_two_hits_returns_false():
    """Operators on soft-wrapping terminals may bump the threshold."""
    # Arrange
    pane = "> Repeat abc123\nReply: abc123\n"
    # Act
    result = pane_has_nonce_echo(pane, "abc123", min_occurrences=3)
    # Assert
    assert result is False


# ── Busy detection ───────────────────────────────────────────────────────────


def test_pane_is_busy_with_empty_pane_returns_false():
    # Arrange
    pane = ""
    # Act
    result = pane_is_busy(pane)
    # Assert
    assert result is False


@pytest.mark.parametrize(
    "pane",
    [
        "... Working\u2026 ...",
        "status: Ruminating\u2026",
        "Processing... (esc to interrupt)",
    ],
)
def test_pane_is_busy_with_default_busy_marker_returns_true(pane):
    """Spot-check the three highest-signal default markers — the
    'esc to interrupt' line is what Claude Code prints while
    generating; 'Working…' / 'Ruminating…' are the tmux status
    spinners."""
    # Arrange
    # (parameterised over canonical busy lines)
    # Act
    result = pane_is_busy(pane)
    # Assert
    assert result is True


def test_pane_is_busy_with_quiet_prompt_only_pane_returns_false():
    # Arrange
    pane = "> \nbypass permissions active\n"
    # Act
    result = pane_is_busy(pane)
    # Assert
    assert result is False


def test_pane_is_busy_with_old_marker_outside_tail_window_returns_false():
    """A historical 'Working…' that scrolled away must not count.
    The 2000-char tail window strips the old marker."""
    # Arrange
    old = "Working\u2026 earlier\n"
    huge_noise = "x" * 5_000
    new_tail = "\nquiet now\n> \n"
    pane = old + huge_noise + new_tail
    # Act
    result = pane_is_busy(pane, tail_chars=2_000)
    # Assert
    assert result is False


def test_pane_is_busy_with_non_default_word_and_default_markers_returns_false():
    """'mulling' is not a default busy marker."""
    # Arrange
    pane = "Hmm… mulling"
    # Act
    result = pane_is_busy(pane)
    # Assert
    assert result is False


def test_pane_is_busy_with_custom_markers_overriding_default_returns_true():
    """Callers can pass a tighter / looser marker list."""
    # Arrange
    pane = "Hmm… mulling"
    # Act
    result = pane_is_busy(pane, markers=("mulling",))
    # Assert
    assert result is True


# ── Single-capture classifier ────────────────────────────────────────────────


def test_classify_probe_with_echo_and_timeout_and_busy_returns_alive():
    """ALIVE wins even at timeout and even if the pane looks busy —
    the echo already proved the agent is responsive."""
    # Arrange
    pane = "> Repeat abc\nabc\nWorking\u2026\n"
    # Act
    state = classify_probe(pane, "abc", is_timeout=True)
    # Assert
    assert state is ProbeState.ALIVE


def test_classify_probe_with_no_echo_and_no_timeout_returns_pending():
    # Arrange
    pane = "> Repeat abc\n"
    # Act
    state = classify_probe(pane, "abc", is_timeout=False)
    # Assert
    assert state is ProbeState.PENDING


def test_classify_probe_with_no_echo_at_timeout_and_busy_marker_returns_busy():
    """Timeout + no echo + pane actively working -> BUSY.
    Caller should defer re-probing rather than declare silent."""
    # Arrange
    pane = "> Repeat abc\n... Working\u2026 ...\n"
    # Act
    state = classify_probe(pane, "abc", is_timeout=True)
    # Assert
    assert state is ProbeState.BUSY


def test_classify_probe_with_no_echo_at_timeout_and_quiet_pane_returns_silent():
    """Timeout + no echo + quiet pane -> SILENT (agent looks frozen)."""
    # Arrange
    pane = "> Repeat abc\n(nothing else)\n"
    # Act
    state = classify_probe(pane, "abc", is_timeout=True)
    # Assert
    assert state is ProbeState.SILENT


# ── Polling loop helpers (real fakes, not mocks) ─────────────────────────────


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


def _run(captures, *, timeout=10.0, poll_interval=2.0, nonce="dead1234"):
    """Drive ``wait_for_nonce_echo`` with a deterministic clock and a
    scripted capture function. Returns ``(result, capture, clock)``."""
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


# ── Polling loop — happy path ────────────────────────────────────────────────


def test_wait_for_nonce_echo_when_first_capture_has_echo_returns_alive():
    # Arrange
    prompt_with_echo = "> Repeat dead1234\nEcho: dead1234\n"
    # Act
    (state, _elapsed), _cap, _clk = _run([prompt_with_echo])
    # Assert
    assert state is ProbeState.ALIVE


def test_wait_for_nonce_echo_when_first_capture_has_echo_reports_zero_elapsed():
    # Arrange
    prompt_with_echo = "> Repeat dead1234\nEcho: dead1234\n"
    # Act
    (_state, elapsed), _cap, _clk = _run([prompt_with_echo])
    # Assert
    assert elapsed == 0.0


def test_wait_for_nonce_echo_when_first_capture_has_echo_polls_exactly_once():
    # Arrange
    prompt_with_echo = "> Repeat dead1234\nEcho: dead1234\n"
    # Act
    (_state, _elapsed), cap, _clk = _run([prompt_with_echo])
    # Assert
    assert cap.calls == 1


def test_wait_for_nonce_echo_when_echo_appears_after_two_polls_returns_alive():
    """Typical path: first 2 captures show prompt only, 3rd shows echo."""
    # Arrange
    prompt = "> Repeat dead1234\n"
    echoed = prompt + "dead1234\n"
    # Act
    (state, _elapsed), _cap, _clk = _run([prompt, prompt, echoed], poll_interval=2.0)
    # Assert
    assert state is ProbeState.ALIVE


def test_wait_for_nonce_echo_when_echo_appears_after_two_polls_reports_four_seconds_elapsed():
    """Two sleeps between three captures, 2s each -> 4s elapsed."""
    # Arrange
    prompt = "> Repeat dead1234\n"
    echoed = prompt + "dead1234\n"
    # Act
    (_state, elapsed), _cap, _clk = _run([prompt, prompt, echoed], poll_interval=2.0)
    # Assert
    assert elapsed == 4.0


def test_wait_for_nonce_echo_when_echo_appears_after_two_polls_takes_three_captures():
    # Arrange
    prompt = "> Repeat dead1234\n"
    echoed = prompt + "dead1234\n"
    # Act
    (_state, _elapsed), cap, _clk = _run([prompt, prompt, echoed], poll_interval=2.0)
    # Assert
    assert cap.calls == 3


# ── Polling loop — timeout paths ─────────────────────────────────────────────


def test_wait_for_nonce_echo_when_pane_quiet_until_timeout_returns_silent():
    """Pane never changes, no busy marker -> SILENT at deadline."""
    # Arrange
    prompt_only = "> Repeat dead1234\n"
    # Act
    (state, _elapsed), _cap, _clk = _run([prompt_only], timeout=6.0, poll_interval=2.0)
    # Assert
    assert state is ProbeState.SILENT


def test_wait_for_nonce_echo_when_pane_quiet_until_timeout_reports_at_least_timeout_elapsed():
    """Loop: t=0 poll, sleep 2 -> t=2, poll, sleep 2 -> t=4, poll, sleep 2
    -> t=6 deadline, final poll classifies silent."""
    # Arrange
    prompt_only = "> Repeat dead1234\n"
    # Act
    (_state, elapsed), _cap, _clk = _run([prompt_only], timeout=6.0, poll_interval=2.0)
    # Assert
    assert elapsed >= 6.0


def test_wait_for_nonce_echo_when_pane_shows_busy_marker_until_timeout_returns_busy():
    """Pane shows 'Working…' throughout -> BUSY at deadline."""
    # Arrange
    busy_pane = "> Repeat dead1234\nWorking\u2026\n"
    # Act
    (state, _elapsed), _cap, _clk = _run([busy_pane], timeout=4.0, poll_interval=2.0)
    # Assert
    assert state is ProbeState.BUSY


# ── Polling loop — short-circuit and resilience ──────────────────────────────


def test_wait_for_nonce_echo_when_echo_arrives_first_poll_does_not_sleep():
    """Don't keep polling after the echo arrives — exactly one capture
    and zero clock advance."""
    # Arrange
    echoed = "> Repeat dead1234\ndead1234 — done\n"
    # Act
    (_state, _elapsed), _cap, clk = _run([echoed], timeout=60.0, poll_interval=2.0)
    # Assert
    assert clk.t == 0.0


def test_wait_for_nonce_echo_when_echo_arrives_first_poll_makes_one_capture():
    # Arrange
    echoed = "> Repeat dead1234\ndead1234 — done\n"
    # Act
    (_state, _elapsed), cap, _clk = _run([echoed], timeout=60.0, poll_interval=2.0)
    # Assert
    assert cap.calls == 1


@pytest.mark.parametrize(
    "marker",
    ["Working\u2026", "Ruminating\u2026", "esc to interrupt"],
)
def test_default_busy_markers_contains_known_indicator(marker):
    """Spot-check a couple of the markers the default list should cover."""
    # Arrange
    # (parameterised over the canonical busy markers we rely on)
    # Act
    present = marker in DEFAULT_BUSY_MARKERS
    # Assert
    assert present is True


class _NoneCapture:
    """Capture callable that always returns ``None`` — exercises the
    'capture returned nothing' branch of the polling loop."""

    def __init__(self):
        self.calls = 0

    def __call__(self, _t):
        self.calls += 1
        return None  # type: ignore[return-value]


def test_wait_for_nonce_echo_when_capture_fn_returns_none_treats_pane_as_empty_and_silents():
    """Empty pane -> not busy -> SILENT at deadline."""
    # Arrange
    clk = _FakeClock()
    cap = _NoneCapture()
    # Act
    state, _elapsed = wait_for_nonce_echo(
        agent_name="t",
        pane_target="p",
        nonce="dead1234",
        poll_interval=1.0,
        timeout=2.0,
        capture_fn=cap,  # type: ignore[arg-type]
        time_fn=clk.now,
        sleep_fn=clk.sleep,
    )
    # Assert
    assert state is ProbeState.SILENT


class _FlakyCapture:
    """Capture callable that raises on the first call and then returns
    a pane containing the echoed nonce. Models a transient tmux
    failure that the polling loop must swallow."""

    def __init__(self):
        self.calls = 0

    def __call__(self, _t):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("tmux went away briefly")
        return "> Repeat dead1234\ndead1234\n"


def test_wait_for_nonce_echo_when_capture_fn_raises_once_then_succeeds_returns_alive():
    """A transient tmux failure should not abort the loop."""
    # Arrange
    clk = _FakeClock()
    cap = _FlakyCapture()
    # Act
    state, _elapsed = wait_for_nonce_echo(
        agent_name="t",
        pane_target="p",
        nonce="dead1234",
        poll_interval=1.0,
        timeout=10.0,
        capture_fn=cap,
        time_fn=clk.now,
        sleep_fn=clk.sleep,
    )
    # Assert
    assert state is ProbeState.ALIVE


def test_wait_for_nonce_echo_when_capture_fn_raises_once_then_succeeds_retries_exactly_once():
    """First call raised, second call returned the echo -> 2 calls total."""
    # Arrange
    clk = _FakeClock()
    cap = _FlakyCapture()
    # Act
    wait_for_nonce_echo(
        agent_name="t",
        pane_target="p",
        nonce="dead1234",
        poll_interval=1.0,
        timeout=10.0,
        capture_fn=cap,
        time_fn=clk.now,
        sleep_fn=clk.sleep,
    )
    # Assert
    assert cap.calls == 2
