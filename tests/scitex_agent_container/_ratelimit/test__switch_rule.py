"""Tests for ``_ratelimit._switch_rule`` — may sac change THIS agent's model?

Pure, so every leg is driven by passing facts and a clock in. No mocks and
nothing to mock.

The three claims this file has to make good on, because they are the three
ways an automatic model switcher goes wrong:

* it FIRES on the measured case — a Fable-family agent frozen behind a cap;
* it is IDEMPOTENT — an agent already on the target is never retyped into;
* it does not fire TWICE for one incarnation — the caller's memory of a
  switch it has not yet seen the result of HOLDS the second attempt.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from datetime import datetime, timezone

from scitex_agent_container._ratelimit._modelcap import observe_model_cap
from scitex_agent_container._ratelimit._rule import Verdict
from scitex_agent_container._ratelimit._switch_rule import TARGET_MODEL, decide_switch

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)

FABLE_PANE = "\n".join(
    [
        "● Reading the spec...",
        "  ⎿ You've reached your Fable limit. Run /usage-credits to continue "
        "or switch models with /model.",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)
CLEAN_PANE = "\n".join(["● Done.", "──────────────", "❯ "])
#: The same banner one line further down — a pane that is still producing
#: output, i.e. an agent working (or quoting the incident in prose).
MOVED_PANE = "● Extra output line\n" + FABLE_PANE


def _observe(pane):
    return observe_model_cap(pane, now=NOW, default_tz=timezone.utc)


def _decide(
    *,
    pane=FABLE_PANE,
    second_pane=None,
    spec_model="claude-fable-5",
    policy="on-failure",
    session_present=True,
    already_switched=False,
):
    return decide_switch(
        name="alpha",
        policy=policy,
        session_present=session_present,
        spec_model=spec_model,
        first=_observe(pane),
        second=_observe(second_pane if second_pane is not None else pane),
        now=NOW,
        already_switched=already_switched,
    )


# --- FIRE: the measured case -----------------------------------------------


def test_a_frozen_fable_cap_fires_the_switch() -> None:
    # Arrange — the 2026-09-06 case exactly: a Fable-family agent frozen
    # behind its own cap banner, which publishes no reset, so the rate-wall
    # remedy has nothing to wait for and the agent stays silent forever.
    # Act
    decision = _decide()
    # Assert
    assert decision.verdict is Verdict.SWITCH_MODEL


def test_the_fired_decision_names_the_target() -> None:
    # Arrange — the target is the mutation's argument, so a decision that
    # fires without naming one would be an authorisation to type nothing.
    # Act
    decision = _decide()
    # Assert
    assert decision.target == TARGET_MODEL


def test_the_bare_fable_alias_also_fires() -> None:
    # Arrange — a spec may carry the bare alias rather than the versioned id.
    # Both are the same agent on the same capped family.
    # Act
    decision = _decide(spec_model="fable")
    # Assert
    assert decision.verdict is Verdict.SWITCH_MODEL


def test_the_session_cap_fires_on_fable() -> None:
    # Arrange — the OTHER measured banner, the one the three dead workflow
    # subagents came back with. It publishes a reset hours away; on a Fable
    # agent the switch ends it in seconds.
    pane = "  ⎿ You’ve hit your session limit · resets 2am (UTC)\n❯ "
    # Act
    decision = _decide(pane=pane)
    # Assert
    assert decision.verdict is Verdict.SWITCH_MODEL


# --- IDEMPOTENCE -----------------------------------------------------------


def test_an_agent_on_the_target_is_left_alone() -> None:
    # Arrange — an agent already on the opus family. Typing /model opus[1m]
    # at it would spend three keystrokes and a real turn to change nothing.
    # Act
    decision = _decide(spec_model="claude-opus-4-8[1m]")
    # Assert
    assert decision.verdict is Verdict.ALREADY_ON_TARGET


def test_an_agent_on_the_target_does_not_fire() -> None:
    # Arrange — the same reading, asserted through the property the pass
    # actually branches on, so a verdict rename cannot quietly re-arm it.
    # Act
    decision = _decide(spec_model="opus[1m]")
    # Assert
    assert decision.fires is False


def test_a_non_fable_agent_keeps_todays_verdict() -> None:
    # Arrange — a Sonnet agent behind the SESSION wall. That wall publishes
    # its own end and the rate-wall rule already waits it out correctly;
    # "get off the capped model" means nothing here, so today's verdict must
    # stand rather than being overridden by a switch nobody can justify.
    pane = "  ⎿ You’ve hit your session limit · resets 2am (UTC)\n❯ "
    # Act
    decision = _decide(spec_model="claude-sonnet-4-5", pane=pane)
    # Assert
    assert decision.reason == "not-a-capped-family"


def test_the_banner_outranks_a_stale_spec() -> None:
    # Arrange — the spec says Sonnet and the PANE says Fable. A spec records
    # what the agent was LAUNCHED on and can lag a switch made in the TUI, so
    # the screen wins: the agent visibly capped on Fable is on Fable.
    # Act
    decision = _decide(spec_model="claude-sonnet-4-5", pane=FABLE_PANE)
    # Assert
    assert decision.verdict is Verdict.SWITCH_MODEL


# --- NO SECOND FIRE --------------------------------------------------------


def test_an_already_switched_agent_is_held() -> None:
    # Arrange — we switched this agent and its pane has not advanced since,
    # so the first switch's outcome is not yet visible. Firing again would
    # type a second /model into a pane that may still show the first picker.
    # Act
    decision = _decide(already_switched=True)
    # Assert
    assert decision.verdict is Verdict.COOLING_DOWN


def test_an_already_switched_agent_does_not_fire() -> None:
    # Arrange — the same hold, asserted through the branch the pass reads.
    # Act
    decision = _decide(already_switched=True)
    # Assert
    assert decision.fires is False


def test_an_advancing_pane_is_never_switched() -> None:
    # Arrange — the banner MOVED between the two reads, so output is still
    # being produced. That is an agent working, and this remedy's false
    # positive is expensive: it would change the model of a healthy agent.
    # Act
    decision = _decide(pane=FABLE_PANE, second_pane=MOVED_PANE)
    # Assert
    assert decision.verdict is Verdict.MOVING


# --- the population, and the refusals to guess ------------------------------


def test_an_unmanaged_agent_is_not_ours() -> None:
    # Arrange — restart.policy=never. sac never promised to keep this agent
    # running, so its model is not sac's to change.
    # Act
    decision = _decide(policy="never")
    # Assert
    assert decision.verdict is Verdict.NOT_MANAGED


def test_a_blind_session_read_refuses_to_guess() -> None:
    # Arrange — the tmux enumeration itself failed. "We do not know whether
    # this agent has a session" is not "it has none", and neither is a
    # licence to type into a pane.
    # Act
    decision = _decide(session_present=None)
    # Assert
    assert decision.verdict is Verdict.UNREADABLE


def test_an_uncapped_pane_has_nothing_to_switch() -> None:
    # Arrange — the negative control at the rule layer. A working pane must
    # never reach the mutation.
    # Act
    decision = _decide(pane=CLEAN_PANE)
    # Assert
    assert decision.verdict is Verdict.NOT_LIMITED
