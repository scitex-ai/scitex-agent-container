"""A restarted agent must be told to go and read what it missed.

OPERATOR DIRECTIVE 2026-08-03: when an agent is restarted it should read for
itself the instructions that were dropped.

THE LOSS BEING PINNED: scitex-hub's telegram MCP was dead, so operator messages
were not reaching it. Restarting fixed the rail — and destroyed a message the
operator had sent eight minutes earlier, because it was queued against the
session being replaced. Rail repaired, instruction still lost.

SCOPE, stated so these are not read as more than they are: these test the pure
composition function. The INJECTOR keeps its pre-existing early return on an
explicitly empty ``startup_prompts`` — that guard predates this change and its
rationale was not established, so it was left alone rather than overwritten to
deliver the read-back. An agent deliberately left silent must not suddenly
receive a boot turn. In practice 83 of the fleet's specs carry a boot kick, so
the narrow reading costs no real coverage.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._boot_recovery import (
    MISSED_INPUT_RECOVERY_PROMPT,
    with_missed_input_recovery,
)


def test_the_recovery_prompt_leads():
    # Arrange — it must come FIRST; a read-back after the agent has already
    # started working is a read-back of a decision already taken.
    spec_prompts = ["Start or continue. Scan your scitex-cards slices."]
    # Act
    result = with_missed_input_recovery(spec_prompts)
    # Assert
    assert result[0] == MISSED_INPUT_RECOVERY_PROMPT


def test_the_specs_own_prompts_are_preserved_in_order():
    # Arrange — the boot kick must still work; this adds, never replaces.
    spec_prompts = ["first", "second"]
    # Act
    result = with_missed_input_recovery(spec_prompts)
    # Assert
    assert result[1:] == ["first", "second"]


def test_the_composer_yields_the_recovery_even_for_an_empty_list():
    # Arrange — a property of the COMPOSER, not of the injector: the injector
    # still early-returns on an explicitly empty startup_prompts. Pinned so a
    # future caller that does want the empty case gets defined behaviour.
    spec_prompts = []
    # Act
    result = with_missed_input_recovery(spec_prompts)
    # Assert
    assert result == [MISSED_INPUT_RECOVERY_PROMPT]


def test_none_is_treated_as_no_prompts():
    # Arrange — spec.startup_prompts is absent rather than empty.
    spec_prompts = None
    # Act
    result = with_missed_input_recovery(spec_prompts)
    # Assert
    assert result == [MISSED_INPUT_RECOVERY_PROMPT]


def test_empty_entries_are_dropped_rather_than_injected_as_blank_turns():
    # Arrange — a blank prompt would submit an empty turn on boot.
    spec_prompts = ["", "real", ""]
    # Act
    result = with_missed_input_recovery(spec_prompts)
    # Assert
    assert result == [MISSED_INPUT_RECOVERY_PROMPT, "real"]


def test_the_prompt_does_not_assert_that_messages_were_missed():
    # Arrange — sac cannot know at injection time whether anything was missed,
    # and a prompt that asserts a backlog which may be empty trains the reader
    # to skip it. It must instruct a CHECK, not report a fact.
    text = MISSED_INPUT_RECOVERY_PROMPT
    # Act
    asserts_a_backlog = "you have missed" in text.lower()
    # Assert
    assert not asserts_a_backlog
