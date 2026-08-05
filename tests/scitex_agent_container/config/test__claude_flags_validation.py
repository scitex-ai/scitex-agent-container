"""``spec.claude.flags`` must carry one argv token per element.

INCIDENT 2026-08-06. ``figrecipe`` had been unreachable since 2026-07-22 and
was assumed to be a dead a2a sidecar. It was not: a restart printed

    error: unknown option '--effort ultracode'

Its spec listed ``--effort ultracode`` as ONE element of ``spec.claude.flags``.
Each element becomes one argv token, so claude got that whole string as a
single option name and the inner process exited during boot. Every restart in
those 15 days failed the same way, silently — the agent simply stayed
unreachable, and the real cause was invisible until someone ran a restart and
read the boot stderr.

What makes it worth a validator rather than a fixed spec: nothing about the
YAML looks wrong. ``- --effort ultracode`` reads exactly like a command line.
The list-of-argv-tokens contract is invisible at the point of authoring, and
the failure surfaces only at boot, in a log nobody reads until an agent has
been missing for two weeks.

These tests pin the RULE (which shapes are refused) and, just as importantly,
the shapes that must NOT be refused — a false positive here blocks an agent
boot, which is the same harm in the other direction.

Real ``validate_claude`` on real dicts, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._claude_validation import validate_claude


def _errors_for(flags: object) -> list[str]:
    return validate_claude({"claude": {"model": "haiku", "flags": flags}})


# --------------------------------------------------------------------------
# The incident itself
# --------------------------------------------------------------------------


def test_the_figrecipe_entry_is_rejected() -> None:
    """Regression pin: the exact element that killed figrecipe for 15 days."""
    # Arrange
    flags = ["--dangerously-skip-permissions", "--effort ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("--effort ultracode" in e for e in errors)


def test_the_error_names_the_index_and_offers_the_split() -> None:
    """A refusal must say what to write instead, not merely that it is wrong."""
    # Arrange
    flags = ["--dangerously-skip-permissions", "--effort ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("flags[1]" in e and "--effort" in e and "ultracode" in e for e in errors)


def test_the_offered_split_actually_clears_the_condition() -> None:
    """The hint must produce a VALID spec — a repair hint that still fails is
    worse than none, because it burns the one attempt the author will make."""
    # Arrange — precisely what the error message tells the author to write.
    flags = ["--dangerously-skip-permissions", "--effort", "ultracode"]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


# --------------------------------------------------------------------------
# Shapes that must NOT be refused (false positives block a boot too)
# --------------------------------------------------------------------------


def test_a_bare_value_containing_spaces_is_allowed() -> None:
    """Three live capsule specs pass this exact JSON as a flags element.

    It contains a space, but it is a VALUE — the space is payload and never
    reaches an option parser. Keying the rule on whitespace alone would have
    rejected all three.
    """
    # Arrange
    flags = ["--mcp-config", '{"mcpServers": {}}']
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_the_equals_spelling_with_a_spaced_value_is_allowed() -> None:
    """``--flag=value`` is one legitimate token even when the value has spaces."""
    # Arrange
    flags = ['--mcp-config={"mcpServers": {}}']
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


@pytest.mark.parametrize(
    "flag",
    ["--dangerously-skip-permissions", "--effort", "-v", "--verbose", "ultracode"],
)
def test_ordinary_single_tokens_are_allowed(flag: str) -> None:
    """Control: if any of these were refused, the guard is over-broad."""
    # Arrange
    flags = [flag]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert errors == []


def test_absent_flags_produce_no_error() -> None:
    """Control: the rule must be silent when there is nothing to check."""
    # Arrange
    spec = {"claude": {"model": "haiku"}}
    # Act
    errors = validate_claude(spec)
    # Assert
    assert errors == []


# --------------------------------------------------------------------------
# Shape errors
# --------------------------------------------------------------------------


def test_a_string_instead_of_a_list_is_rejected() -> None:
    """``flags: --effort ultracode`` (no list) is the same mistake, one level up."""
    # Arrange
    flags = "--effort ultracode"
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("must be a list" in e for e in errors)


def test_a_non_string_element_is_rejected() -> None:
    # Arrange
    flags = ["--effort", 3]
    # Act
    errors = _errors_for(flags)
    # Assert
    assert any("flags[1]" in e and "string" in e for e in errors)
