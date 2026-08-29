"""Tests for ``_ratelimit._banner`` — is there a wall, and when does it lift?

Pure, so every leg is driven by passing a pane and a clock in. No mocks and
nothing to mock.

The behaviours that matter, in the order they matter:

* every REAL captured banner parses. These are verbatim specimens, not
  invented strings, and a rendering change that breaks the matcher must break
  a test rather than silently return "no wall" — which reads as a healthy
  fleet during an outage.
* an unparseable reset is ``None``, never a guess. Guessing is how a reviver
  starts hammering a wall that is still up.
* the NEAREST occurrence of a bare clock time is the right reading, and the
  leg that proves it uses the 2026-08-28 incident's own numbers.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scitex_agent_container._ratelimit._banner import (
    SPECIMEN_PANES,
    observe_pane,
    parse_reset_at,
    resolve_clock_near,
)

# 2026-08-28 21:00 UTC — after the incident's 19:10 UTC reset, which is the
# vantage that makes the nearest-occurrence rule answer "the wall is down".
NOW = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)


def _observe(pane: str | None, *, now: datetime = NOW):
    return observe_pane(pane, now=now, default_tz=timezone.utc)


# --- the real specimens: every captured rendering must parse ----------------


@pytest.mark.parametrize("pane", SPECIMEN_PANES)
def test_every_captured_specimen_is_detected(pane: str) -> None:
    # Arrange — verbatim panes recovered from agent transcripts. A matcher
    # that stops recognising one of these reports a healthy fleet during an
    # outage, which is the whole failure this module exists to prevent.
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.limited is True


@pytest.mark.parametrize("pane", SPECIMEN_PANES)
def test_every_captured_specimen_yields_a_reset(pane: str) -> None:
    # Arrange — a detected wall with no reset time cannot be waited out, so
    # detection alone is not enough for any of the shipped renderings.
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.reset_at is not None


def test_the_incident_banner_resolves_to_its_real_reset() -> None:
    # Arrange — the 2026-08-28 banner the operator quoted, read at 21:00 UTC.
    # The provider printed a bare "7:10pm" with no date and no zone; the
    # limit really did lift at 19:10 UTC. This is the positive control for
    # resolve_clock_near: "the NEXT 7:10pm" would answer tomorrow and leave
    # the agent parked for another 22 hours.
    pane = "You’ve hit your session limit · resets 7:10pm"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.reset_at == datetime(2026, 8, 28, 19, 10, tzinfo=timezone.utc)


def test_a_labelled_zone_beats_the_default() -> None:
    # Arrange — "8am (Asia/Tokyo)" is 23:00 UTC the previous day, NOT 08:00
    # UTC. Reading a labelled banner in the sweep's own frame would move the
    # reset by nine hours and wake the agent into a standing wall.
    pane = "You've hit your weekly limit · resets 8am (Asia/Tokyo)"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.reset_at.utcoffset() == timedelta(hours=9)


def test_the_window_word_is_carried_through() -> None:
    # Arrange — session / weekly / usage are different windows and the
    # operator must see which one stopped the agent; a detector that
    # normalised them away would answer "a limit" to "which limit".
    pane = "You've hit your session limit · resets 11pm (UTC)"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.window == "session"


# --- refusals: what must NOT be read as a wall ------------------------------


def test_an_unreadable_pane_is_not_a_clean_pane() -> None:
    # Arrange — a pane we could not capture told us nothing. Reporting
    # "not limited" here is an instrument announcing good news about
    # something it never saw.
    # Act
    observation = _observe(None)
    # Assert
    assert observation.readable is False


def test_agent_prose_about_a_limit_is_not_a_wall() -> None:
    # Arrange — an agent writing ABOUT the incident must never be treated as
    # one parked behind it. The anchor is the provider's own first-person
    # sentence, which prose does not reproduce at the start of a line.
    pane = "figrecipe died behind a weekly limit and resets are not the issue"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.limited is False


def test_a_wall_with_no_reset_clause_yields_no_instant() -> None:
    # Arrange — the wall is real but untimed. The rule above must HOLD on
    # this, so the parser has to hand it up as None rather than inventing a
    # time; a guessed reset is how a reviver burns the quota that would have
    # ended the outage.
    pane = "You've hit your session limit"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.reset_at is None


def test_an_untimed_wall_is_still_detected() -> None:
    # Arrange — the same pane. Failing to PARSE the reset must not also lose
    # the fact that a wall is there; those are two different findings and the
    # second one is what gets reported to a human.
    pane = "You've hit your session limit"
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.limited is True


def test_the_newest_banner_wins_over_an_older_one() -> None:
    # Arrange — a pane is a scrolling log, so a lifted wall sits ABOVE a
    # current one. Reading the first match would answer with a reset that
    # already expired and wake the agent into the newer wall.
    pane = "\n".join(
        [
            "You've hit your weekly limit · resets 1am (UTC)",
            "...work...",
            "You've hit your session limit · resets 11pm (UTC)",
        ]
    )
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.reset_at == datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)


# --- the nearest-occurrence rule, on its own -------------------------------


def test_a_recent_past_clock_resolves_backwards() -> None:
    # Arrange — 19:10 read at 21:00. Nearest is 1h50m behind, not 22h ahead.
    # Act
    resolved = resolve_clock_near(hour=19, minute=10, now=NOW, tz=timezone.utc)
    # Assert
    assert resolved == datetime(2026, 8, 28, 19, 10, tzinfo=timezone.utc)


def test_a_near_future_clock_resolves_forwards() -> None:
    # Arrange — 23:00 read at 21:00. Nearest is 2h ahead, not 22h behind.
    # Act
    resolved = resolve_clock_near(hour=23, minute=0, now=NOW, tz=timezone.utc)
    # Assert
    assert resolved == datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)


def test_a_clock_across_midnight_resolves_to_the_next_day() -> None:
    # Arrange — 01:00 read at 23:30. The nearest 01:00 is 90 minutes ahead on
    # the FOLLOWING date; "today's 01:00" is 22.5 hours behind and wrong.
    late = datetime(2026, 8, 28, 23, 30, tzinfo=timezone.utc)
    # Act
    resolved = resolve_clock_near(hour=1, minute=0, now=late, tz=timezone.utc)
    # Assert
    assert resolved == datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)


def test_an_iso_reset_is_taken_literally() -> None:
    # Arrange — a fully-qualified instant needs no nearest-occurrence guess
    # and must not get one.
    # Act
    instant, _raw = parse_reset_at(
        "resets at 2026-06-18T05:00Z", now=NOW, default_tz=timezone.utc
    )
    # Assert
    assert instant == datetime(2026, 6, 18, 5, 0, tzinfo=timezone.utc)


def test_midnight_meridiem_is_not_read_as_noon() -> None:
    # Arrange — "12am" is 00:00, "12pm" is 12:00. Getting this backwards
    # moves a reset by twelve hours in the direction that wakes an agent into
    # a standing wall.
    # Act
    instant, _raw = parse_reset_at(
        "resets 12am (UTC)", now=NOW, default_tz=timezone.utc
    )
    # Assert
    assert instant.hour == 0
