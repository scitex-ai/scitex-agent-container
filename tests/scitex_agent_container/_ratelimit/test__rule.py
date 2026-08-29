"""Tests for ``_ratelimit._rule`` — may sac wake THIS agent, and is it time?

The rule is pure, so every leg is driven by passing facts in. No mocks and
nothing to mock: no tmux, no clock, no database.

The behaviours that matter, in the order they matter:

* a LIFTED wall on a frozen pane is the resume case, and it is the leg that
  proves this enforcer would have recovered the 2026-08-28 fleet.
* a STANDING wall is HELD and that is a SUCCESS. This is the leg that makes
  hammering a live limit structurally impossible rather than merely unlikely
  — the resume branch is unreachable while ``now < reset_at``.
* an UNREADABLE reset is never guessed at.
* a MOVING pane is a working agent and is never touched. A false positive
  here interrupts something that was fine.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container._ratelimit._banner import LimitObservation
from scitex_agent_container._ratelimit._rule import Verdict, decide

NOW = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)
LIFTED = datetime(2026, 8, 28, 19, 10, tzinfo=timezone.utc)
STANDING = datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)


def _walled(reset_at: datetime | None = LIFTED, *, line: int = 12):
    """A readable pane showing a rate wall at a fixed line."""
    return LimitObservation(
        readable=True,
        limited=True,
        window="session",
        reset_at=reset_at,
        reset_text="resets 7:10pm",
        line_index=line,
        detail="a session rate wall",
    )


def _decide(**overrides):
    """Decide for a managed, live agent frozen behind a LIFTED wall.

    That is the interesting case — every leg below differs from it in
    exactly one fact.
    """
    facts = {
        "name": "alpha",
        "policy": "on-failure",
        "session_present": True,
        "first": _walled(),
        "second": _walled(),
        "now": NOW,
    }
    facts.update(overrides)
    return decide(**facts)


# --- the resume case: the leg that recovers the fleet -----------------------


def test_a_lifted_wall_authorises_a_resume() -> None:
    # Arrange — the exact 2026-08-28 shape: a live session, a frozen banner,
    # and a published reset that has already passed. Nothing inside the agent
    # will notice the wall came down, because the thing that would have
    # noticed IS the turn the wall stopped.
    # Act
    decision = _decide()
    # Assert
    assert decision.verdict is Verdict.RESUME


def test_the_resume_names_the_lifted_wall() -> None:
    # Arrange — a verdict whose cause is not stated is the silent skip this
    # whole enforcer exists to abolish.
    # Act
    decision = _decide()
    # Assert
    assert decision.reason == "wall-lifted"


# --- the hold cases: waiting is a SUCCESS, not a deferred failure -----------


def test_a_standing_wall_is_held_not_resumed() -> None:
    # Arrange — the wall lifts at 23:00 and it is 21:00. This is THE leg that
    # makes hot-looping impossible: the resume branch cannot be reached while
    # the wall stands, so the pass cannot spend a token against a live limit
    # and make the outage longer.
    # Act
    decision = _decide(first=_walled(STANDING), second=_walled(STANDING))
    # Assert
    assert decision.verdict is Verdict.WAITING


def test_an_unreadable_reset_is_held_not_guessed() -> None:
    # Arrange — a real wall we cannot time. Inventing a reset is exactly how
    # a reviver starts hammering a limit that is still in force.
    # Act
    decision = _decide(first=_walled(None), second=_walled(None))
    # Assert
    assert decision.verdict is Verdict.RESET_UNKNOWN


# --- the refusals: who this enforcer is NOT for -----------------------------


def test_an_unmanaged_agent_is_never_touched() -> None:
    # Arrange — restart.policy defaults to "never", so a spec that omits a
    # restart block is not ours. Same opt-in as fleet-reconcile, deliberately:
    # two enforcers with different ideas of who they cover is a gap nobody
    # can see.
    # Act
    decision = _decide(policy="never")
    # Assert
    assert decision.verdict is Verdict.NOT_MANAGED


def test_a_corpse_is_handed_to_fleet_reconcile() -> None:
    # Arrange — no live session means no pane to be walled. A rate wall is a
    # pause in a LIVE session; resurrecting corpses is a different job with a
    # different remedy, and a pass cannot both delegate a case and act on it.
    # Act
    decision = _decide(session_present=False)
    # Assert
    assert decision.verdict is Verdict.NO_SESSION


def test_an_unreadable_session_list_is_not_an_absent_session() -> None:
    # Arrange — "I could not enumerate" and "there is nothing there" lead to
    # opposite conclusions, and sac's other two enforcers already refuse to
    # collapse them. All three must agree or the fleet gets two answers.
    # Act
    decision = _decide(session_present=None)
    # Assert
    assert decision.verdict is Verdict.UNREADABLE


def test_an_uncapturable_pane_is_not_a_working_agent() -> None:
    # Arrange — the decisive read failed. Reporting NOT-LIMITED here would be
    # an instrument announcing good news about a thing it never observed.
    # Act
    decision = _decide(second=LimitObservation(readable=False, detail="no capture"))
    # Assert
    assert decision.verdict is Verdict.UNREADABLE


def test_a_clean_pane_is_nothing_to_do() -> None:
    # Arrange — the overwhelming majority of a healthy fleet.
    # Act
    decision = _decide(second=LimitObservation(readable=True, limited=False))
    # Assert
    assert decision.verdict is Verdict.NOT_LIMITED


def test_a_moving_banner_means_the_agent_is_working() -> None:
    # Arrange — the banner is at a different pane line across the two reads,
    # so output is still being produced. That is an agent working, or one
    # QUOTING the incident in prose; nudging it would interrupt something
    # that was fine. Same freeze discipline as the auth healer.
    # Act
    decision = _decide(first=_walled(line=8), second=_walled(line=12))
    # Assert
    assert decision.verdict is Verdict.MOVING


def test_a_wall_that_only_just_appeared_is_moving() -> None:
    # Arrange — no wall on the first read, one on the second. The pane
    # advanced between the captures, so the agent was producing output during
    # the observation window and has not settled behind the wall yet.
    # Act
    decision = _decide(first=LimitObservation(readable=True, limited=False))
    # Assert
    assert decision.verdict is Verdict.MOVING


# --- ordering: ownership is asked before liveness ---------------------------


def test_policy_is_checked_before_the_pane() -> None:
    # Arrange — an unmanaged agent behind a lifted wall on an unreadable
    # pane. "Is it ours?" precedes every other question, so this must answer
    # NOT-MANAGED rather than UNREADABLE; reading pane evidence for an agent
    # we would never act on can only produce noise.
    # Act
    decision = _decide(
        policy="never",
        first=LimitObservation(readable=False),
        second=LimitObservation(readable=False),
    )
    # Assert
    assert decision.verdict is Verdict.NOT_MANAGED
