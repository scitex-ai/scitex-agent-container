"""Tests for ``_ratelimit._modelcap`` — is this a wall a model switch can end?

Pure, so every leg is driven by passing a pane and a clock in. No mocks and
nothing to mock.

The behaviours that matter, in the order they matter:

* BOTH measured banners parse. These are the verbatim strings the operator's
  messages and the three dead workflow subagents came back with on
  2026-09-06, and a matcher fitted to one of them would report a healthy
  fleet through an outage of the other.
* an ordinary pane does NOT match. Without that negative control a matcher
  that returns True for everything passes every positive test above.
* an uncapturable pane is ``readable=False``, never ``capped=False``.
* :func:`verify_switch` never certifies a switch from sac's OWN keystrokes.
  sac types the target model into the very pane it then reads, so a verifier
  without that subtraction would certify every attempt, successful or not.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scitex_agent_container._ratelimit._modelcap import (
    CAPPED_SPECIMEN_PANES,
    observe_model_cap,
    verify_switch,
)
from scitex_agent_container._ratelimit._switch import KICK_MESSAGE, model_command

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)

#: The Fable cap exactly as the harness answered the operator, in the pane
#: position it really occupied: last conversation line, above the prompt box.
FABLE_PANE = "\n".join(
    [
        "● Reading the spec...",
        "  ⎿ You've reached your Fable limit. Run /usage-credits to continue "
        "or switch models with /model.",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)

#: The shape the three workflow subagents died behind in the same window.
SESSION_PANE = "\n".join(
    [
        "● Working on the migration...",
        "  ⎿ You’ve hit your session limit · resets 2am (UTC)",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)

#: The NEGATIVE CONTROL. An ordinary working pane, including an agent talking
#: ABOUT limits in prose — the text that must never be mistaken for the
#: provider's own first-person banner.
ORDINARY_PANE = "\n".join(
    [
        "● The switcher fires when an agent hits the Fable limit.",
        "  ⎿ Wrote src/scitex_agent_container/_ratelimit/_switch.py",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)


def _observe(pane: str | None):
    return observe_model_cap(pane, now=NOW, default_tz=timezone.utc)


# --- the real specimens: both measured renderings must parse ----------------


@pytest.mark.parametrize("pane", CAPPED_SPECIMEN_PANES)
def test_every_measured_cap_banner_is_detected(pane: str) -> None:
    # Arrange — the verbatim strings from 2026-09-06. A matcher that stops
    # recognising one of these reports a healthy fleet during the outage it
    # was written for.
    # Act
    observation = _observe(pane)
    # Assert
    assert observation.capped is True


def test_the_fable_banner_names_its_subject() -> None:
    # Arrange — the subject word is what the rule above reads to decide
    # whether the cap is on a switchable model family.
    # Act
    observation = _observe(FABLE_PANE)
    # Assert
    assert observation.subject == "fable"


def test_the_fable_banner_publishes_no_reset() -> None:
    # Arrange — this is the whole reason the switch remedy exists: there is
    # no end time to wait for, so the waiting machinery has nothing to do and
    # the agent stays silent forever.
    # Act
    observation = _observe(FABLE_PANE)
    # Assert
    assert observation.reset_at is None


def test_the_fable_banner_offers_the_model_remedy() -> None:
    # Arrange — the provider itself prints the way out. Recording that makes
    # the observation self-describing rather than sac asserting a remedy the
    # screen never mentioned.
    # Act
    observation = _observe(FABLE_PANE)
    # Assert
    assert observation.remedy_offered is True


def test_the_session_banner_is_also_a_cap() -> None:
    # Arrange — the second measured shape. It carries a reset clause, so the
    # rate-wall rule can act on it too; which remedy applies is the rule's
    # question, not this parser's.
    # Act
    observation = _observe(SESSION_PANE)
    # Assert
    assert observation.subject == "session"


def test_the_session_banner_reset_is_parsed() -> None:
    # Arrange — reusing ``_banner.parse_reset_at`` rather than growing a
    # second reset parser is only defensible if it actually works here.
    # Act
    observation = _observe(SESSION_PANE)
    # Assert
    assert observation.reset_at == datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc)


# --- the controls: a working pane, and a pane nobody could read -------------


def test_an_ordinary_pane_is_not_capped() -> None:
    # Arrange — THE NEGATIVE CONTROL. Without it every positive test above
    # would also pass for a matcher that returns True unconditionally, and
    # the switcher would start retyping /model into working agents.
    # Act
    observation = _observe(ORDINARY_PANE)
    # Assert
    assert observation.capped is False


def test_an_unreadable_pane_reports_unreadable() -> None:
    # Arrange — "I could not look" must never be spelled the same way as
    # "I looked and it was fine": the second is good news about something
    # nobody saw.
    # Act
    observation = _observe(None)
    # Assert
    assert observation.readable is False


def test_an_unreadable_pane_says_no_evidence() -> None:
    # Arrange — the detail is what an operator reads in the timer log, so a
    # blind read must SAY it was blind rather than leaving a bare False that
    # looks like a clean answer.
    # Act
    observation = _observe(None)
    # Assert
    assert "NO evidence" in observation.detail


# --- verification: never certify a switch from our own keystrokes -----------


def _verify(pane, *, kick_submitted=None, target="opus[1m]"):
    return verify_switch(
        pane,
        target_model=target,
        sent_texts=(model_command(target), KICK_MESSAGE),
        kick_submitted=kick_submitted,
        now=NOW,
        default_tz=timezone.utc,
    )


def test_a_still_capped_pane_is_not_switched() -> None:
    # Arrange — all three steps were typed and the wall is still on screen.
    # That is a proven failure, not an ambiguity.
    # Act
    evidence = _verify(FABLE_PANE, kick_submitted=True)
    # Assert
    assert evidence.switched is False


def test_our_own_echo_never_proves_a_switch() -> None:
    # Arrange — THE SELF-MATCH TRAP. The pane holds nothing but sac's own
    # "/model opus[1m]" keystrokes, and the kick was not provably submitted.
    # A verifier that simply searched for the target would certify this.
    pane = "\n".join(["❯ /model opus[1m]", "──────────────", "❯ "])
    # Act
    evidence = _verify(pane, kick_submitted=None)
    # Assert
    assert evidence.switched is None


def test_the_target_beyond_our_echo_proves_it() -> None:
    # Arrange — the command echo PLUS a second, independent naming of the
    # model. Only occurrences beyond the ones sac contributed are evidence.
    pane = "\n".join(
        ["❯ /model opus[1m]", "  ⎿ Set model to opus[1m]", "──────────────", "❯ "]
    )
    # Act
    evidence = _verify(pane, kick_submitted=None)
    # Assert
    assert evidence.switched is True


def test_a_proven_kick_on_a_clean_pane_counts() -> None:
    # Arrange — the cap is gone and the kick was PROVEN to leave the compose
    # box. A capped agent cannot accept a turn, so this is a working agent.
    # Act
    evidence = _verify(ORDINARY_PANE, kick_submitted=True)
    # Assert
    assert evidence.switched is True


def test_an_unsubmitted_kick_proves_nothing() -> None:
    # Arrange — the banner is gone but nothing shows the switch took. This
    # must be an ambiguity a human looks at, never a claimed recovery.
    # Act
    evidence = _verify(ORDINARY_PANE, kick_submitted=False)
    # Assert
    assert evidence.switched is None


def test_an_uncapturable_pane_verifies_nothing() -> None:
    # Arrange — we could not look after the switch. A send that returned 0 is
    # not a model change, so blindness here must stay blindness.
    # Act
    evidence = _verify(None, kick_submitted=True)
    # Assert
    assert evidence.switched is None


class TestTheSessionsOwnConfirmationLine:
    """The operator's fourth step: trust what the SESSION printed, not what sac typed.

    2026-09-06, verbatim in substance: "the Set model to ... line would be
    useful for final confirmation". It is the strongest rung available because
    sac types ``/model opus[1m]`` and never the phrase "Set model to", so this
    evidence cannot be sac's own echo — which is the trap every other rung of
    this ladder has to do arithmetic to avoid.
    """

    def test_the_confirmation_line_proves_the_switch(self) -> None:
        # Arrange
        pane = "❯ /model opus[1m]\n  Set model to Opus 5 (1M context) opus[1m]\n❯ "
        # Act
        evidence = verify_switch(
            pane,
            target_model="opus[1m]",
            sent_texts=("/model opus[1m]",),
            kick_submitted=None,
            now=NOW,
        )
        # Assert
        assert evidence.switched is True

    def test_it_outranks_the_echo_arithmetic(self) -> None:
        # Arrange — the target appears ONLY as sac's own echo, which the
        # counting rung would refuse; the confirmation line still decides.
        pane = "❯ /model opus[1m]\n  Set model to opus[1m]\n❯ "
        # Act
        evidence = verify_switch(
            pane,
            target_model="opus[1m]",
            sent_texts=("/model opus[1m]", "Set model to opus[1m]"),
            kick_submitted=False,
            now=NOW,
        )
        # Assert
        assert evidence.switched is True

    def test_a_confirmation_naming_another_model_is_not_our_switch(self) -> None:
        # Arrange — the session confirms a DIFFERENT model; the target is absent.
        pane = "❯ /model haiku\n  Set model to haiku\n❯ "
        # Act
        evidence = verify_switch(
            pane,
            target_model="opus[1m]",
            sent_texts=("/model opus[1m]",),
            kick_submitted=None,
            now=NOW,
        )
        # Assert
        assert evidence.switched is None
