"""Tests for ``_ratelimit._switch`` — the operator's three steps, in order.

No mocks: every collaborator is a production keyword argument with a real
default, and these tests pass plain recording callables into them. Nothing
sleeps — the clock is injected, and the fake sleep ADVANCES it, so a nine-
second sequence is asserted in microseconds and cannot flake.

What has to be true, and why each one is here:

* THREE steps, in the operator's order (``/model opus[1m]`` -> confirm ->
  kick). His mechanism, 2026-09-06, and a switcher that reorders them types
  a prompt into a model picker.
* THREE SECONDS between them. His number, his words: *"between the three
  steps, i think we should place three seconds for safety"*. It is also the
  settle that stops the Ink TUI eating the Enter that follows a paste.
* the VERIFICATION runs, and its answer — not the send's exit status — is
  what comes back. ``tmux send-keys`` returning 0 means tmux accepted a
  keystroke, never that a model changed.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container._ratelimit._switch import (
    SWITCH_STEP_DELAY_S,
    switch_model_now,
)

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)

CLEAN_PANE = "\n".join(["● Ready.", "──────────────", "❯ "])
STILL_CAPPED_PANE = "\n".join(
    [
        "  ⎿ You've reached your Fable limit. Run /usage-credits to continue "
        "or switch models with /model.",
        "❯ ",
    ]
)


class FakeClock:
    """A real clock callable that only moves when someone sleeps.

    Not a mock: two plain methods with the production signatures
    (``clock() -> float``, ``sleep(seconds) -> None``). Because ``sleep``
    advances ``t``, the timestamps the module stamps on its steps ARE the
    spacing it asked for, and a test can read them without waiting.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class Pane:
    """A real pane that records what was typed into it and what was read."""

    def __init__(self, *, after: str | None = CLEAN_PANE) -> None:
        self.pasted: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.kicks: list[tuple[str, str]] = []
        self.captured: list[str] = []
        self._after = after

    def paste(self, session: str, text: str) -> None:
        self.pasted.append((session, text))

    def send_keys(self, session: str, key: str) -> None:
        self.keys.append((session, key))

    def capture(self, session: str) -> str | None:
        self.captured.append(session)
        return self._after


def _kick(result: bool | None):
    def kick(agent: str, message: str) -> bool | None:
        kick.calls.append((agent, message))
        return result

    kick.calls = []  # type: ignore[attr-defined]
    return kick


def _run(*, pane: Pane, clock: FakeClock, kick_result: bool | None = True):
    return switch_model_now(
        "alpha",
        paste_fn=pane.paste,
        send_keys_fn=pane.send_keys,
        kick_fn=_kick(kick_result),
        capture_fn=pane.capture,
        sleep_fn=clock.sleep,
        clock_fn=clock.clock,
        now=NOW,
    )


# --- the three steps, in the operator's order -------------------------------


def test_the_three_steps_run_in_order() -> None:
    # Arrange — his mechanism verbatim in substance: "1. /model opus[1m]
    # needed 2. Enter or "1" needed to confirm 3. kick needed after the model
    # switch fixed". A switcher that reorders these types a prompt into a
    # model picker.
    pane, clock = Pane(), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock)
    # Assert
    assert [step.name for step in outcome.steps] == [
        "model-command",
        "confirm",
        "kick",
        "verify",
    ]


def test_step_one_pastes_the_model_command() -> None:
    # Arrange — the text must reach the composer LITERALLY and with the
    # slash first: a slash command is only a command when the slash is the
    # first character in the box.
    pane, clock = Pane(), FakeClock()
    # Act
    _run(pane=pane, clock=clock)
    # Assert
    assert pane.pasted == [("tui-alpha", "/model opus[1m]")]


def test_step_two_sends_a_named_enter() -> None:
    # Arrange — a SEPARATE named key, never "-l", which would type the five
    # characters "Enter" into the box.
    pane, clock = Pane(), FakeClock()
    # Act
    _run(pane=pane, clock=clock)
    # Assert
    assert pane.keys == [("tui-alpha", "Enter")]


def test_step_three_kicks_the_agent() -> None:
    # Arrange — the kick is what makes the switch worth doing: nothing
    # inside the agent will notice the model changed, because the thing that
    # would have noticed is the turn the cap stopped.
    pane, clock = Pane(), FakeClock()
    kick = _kick(True)
    # Act
    switch_model_now(
        "alpha",
        paste_fn=pane.paste,
        send_keys_fn=pane.send_keys,
        kick_fn=kick,
        capture_fn=pane.capture,
        sleep_fn=clock.sleep,
        clock_fn=clock.clock,
        now=NOW,
    )
    # Assert
    assert [agent for agent, _ in kick.calls] == ["alpha"]


# --- three seconds between them, on an injected clock -----------------------


def test_the_steps_are_three_seconds_apart() -> None:
    # Arrange — the operator's own number. Asserted on the timestamps the
    # module stamped, so this proves the SPACING and not merely that sleep
    # was called; the fake clock only moves when something sleeps.
    pane, clock = Pane(), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock)
    # Assert
    assert [step.at for step in outcome.steps] == [0.0, 3.0, 6.0, 9.0]


def test_every_gap_uses_the_named_delay() -> None:
    # Arrange — three gaps, one constant. Inlining the number anywhere would
    # let a future edit move one gap and not the others.
    pane, clock = Pane(), FakeClock()
    # Act
    _run(pane=pane, clock=clock)
    # Assert
    assert clock.slept == [SWITCH_STEP_DELAY_S] * 3


# --- the verification is the answer, not the send's exit status -------------


def test_the_verification_capture_runs() -> None:
    # Arrange — the last thing this module does is LOOK. Without this the
    # outcome would be an assumption dressed as a measurement.
    pane, clock = Pane(), FakeClock()
    # Act
    _run(pane=pane, clock=clock)
    # Assert
    assert pane.captured == ["tui-alpha"]


def test_a_clean_pane_and_proven_kick_is_switched() -> None:
    # Arrange — the cap is gone and the kick was PROVEN to leave the compose
    # box. A capped agent cannot accept a turn, so this is a working agent.
    pane, clock = Pane(), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock)
    # Assert
    assert outcome.switched is True


def test_a_still_capped_pane_is_not_switched() -> None:
    # Arrange — every step was typed and the wall is still on screen. That is
    # a proven failure and must be reported as one, not retried forever.
    pane, clock = Pane(after=STILL_CAPPED_PANE), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock)
    # Assert
    assert outcome.switched is False


def test_an_unprovable_switch_is_unknown() -> None:
    # Arrange — the banner is gone, the kick was not provably submitted, and
    # nothing on the pane names the target beyond sac's own keystrokes. THE
    # CLAIM THIS FILE EXISTS FOR: a send returning 0 is not a model change.
    pane, clock = Pane(), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock, kick_result=None)
    # Assert
    assert outcome.switched is None


def test_a_blind_capture_verifies_nothing() -> None:
    # Arrange — we could not read the pane afterwards. Blindness must stay
    # blindness: the operator's agent is either working or silent, and this
    # says we do not know which.
    pane, clock = Pane(after=None), FakeClock()
    # Act
    outcome = _run(pane=pane, clock=clock)
    # Assert
    assert outcome.switched is None
