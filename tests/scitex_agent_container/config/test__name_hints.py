"""Name-miss suggestions shared by attach/start/stop/restart.

Operator order TG61 (card sac-attach-agent-name-hints-20260919): a miss
suggests real agent names via regexp with the exact command per
candidate; suggestions only, never auto-act. One helper so the four
verbs cannot disagree. STX-TQ002 AAA, STX-TQ007 one assert, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._name_hints import suggest_agent_names


def test_regexp_fragment_matches_across_names():
    # Arrange
    names = ("scitex-scholar", "scitex-dev", "scitex-storage")
    # Act
    hint = suggest_agent_names("sch.*r", names, verb="stop")
    # Assert
    assert hint == " Did you mean: `sac agents stop scitex-scholar`?"


def test_invalid_regexp_falls_back_to_literal_without_traceback():
    # Arrange
    names = ("scitex-scholar", "scitex-dev")
    # Act
    hint = suggest_agent_names("sch[", names, verb="start")
    # Assert
    assert hint == ""


def test_literal_dot_matches_only_a_real_dot():
    # Arrange — the fragment is a regexp; '.' matches any char, so both
    # candidates hit, but the exact-name candidate is never suggested
    # back (it names the miss itself, not a correction).
    names = ("scitex-scholar", "scitexXdev")
    # Act
    hint = suggest_agent_names("scitex.dev", names, verb="restart")
    # Assert
    assert hint == " Did you mean: `sac agents restart scitexXdev`?"


def test_many_matches_are_capped_at_limit():
    # Arrange
    names = tuple(f"agent-{i:02d}" for i in range(20))
    # Act
    hint = suggest_agent_names("agent-", names, verb="attach", limit=3)
    # Assert
    assert hint.count("`sac agents attach ") == 3


def test_empty_fragment_matches_nothing():
    # Arrange
    names = ("scitex-scholar",)
    # Act
    hint = suggest_agent_names("", names, verb="stop")
    # Assert
    assert hint == ""


def test_exact_name_is_never_suggested_back():
    # Arrange
    names = ("scitex-scholar", "scitex-dev")
    # Act
    hint = suggest_agent_names("scitex-scholar", names, verb="start")
    # Assert
    assert "scitex-scholar" not in hint


def test_no_match_is_empty_string():
    # Arrange
    names = ("scitex-scholar", "scitex-dev")
    # Act
    hint = suggest_agent_names("xyz-ghost-99", names, verb="restart")
    # Assert
    assert hint == ""


def test_nested_quantifier_falls_back_to_literal():
    # Arrange — catastrophic-backtracking shape must not hang the miss path.
    names = ("scitex-scholar",)
    # Act
    hint = suggest_agent_names("(a+)+$", names, verb="stop")
    # Assert
    assert hint == ""
