"""Tests for ``config._env_var_substitution`` (ADR-0018 PR A).

``resolve_env_var_ref`` classifies a raw string from
``spec.model.<label>.api_key`` into one of three kinds: ``ref`` (env
var reference, returns the bare identifier), ``literal``, or
``empty``. PR A only recognizes the shape; actual ``os.environ``
lookup at agent start lands in PR B.

Edge cases (``$``, ``${}``, ``$1``, lowercase) are LITERALS by
intent — see :mod:`config._env_var_substitution` module docstring on
the uppercase-only choice.

AAA markers (TQ002), descriptive names (TQ003), one assert per test
(TQ007).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._env_var_substitution import (
    is_env_var_ref,
    resolve_env_var_ref,
)

# ---------------------------------------------------------------------------
# Happy-path classification — ref / literal / empty
# ---------------------------------------------------------------------------


def test_dollar_var_form_classifies_as_ref():
    # Arrange / Act
    kind, name = resolve_env_var_ref("$XIAOMI_API_KEY")
    # Assert
    assert kind == "ref"


def test_dollar_var_form_returns_bare_identifier():
    # Arrange / Act
    _, name = resolve_env_var_ref("$XIAOMI_API_KEY")
    # Assert
    assert name == "XIAOMI_API_KEY"


def test_dollar_brace_var_form_classifies_as_ref():
    # Arrange / Act
    kind, name = resolve_env_var_ref("${DEEPSEEK_API_KEY}")
    # Assert
    assert kind == "ref"


def test_dollar_brace_var_form_returns_bare_identifier():
    # Arrange / Act
    _, name = resolve_env_var_ref("${DEEPSEEK_API_KEY}")
    # Assert
    assert name == "DEEPSEEK_API_KEY"


def test_anthropic_api_key_literal_classifies_as_literal():
    # Arrange / Act
    kind, _ = resolve_env_var_ref("sk-ant-xxx")
    # Assert — operator pasted the actual secret into spec.yaml.
    assert kind == "literal"


def test_literal_returns_no_var_name():
    # Arrange / Act
    _, name = resolve_env_var_ref("sk-ant-xxx")
    # Assert
    assert name is None


def test_empty_string_classifies_as_empty():
    # Arrange / Act — empty = field omitted = fall back to registry.
    kind, _ = resolve_env_var_ref("")
    # Assert
    assert kind == "empty"


def test_empty_string_returns_no_var_name():
    # Arrange / Act
    _, name = resolve_env_var_ref("")
    # Assert
    assert name is None


# ---------------------------------------------------------------------------
# Edge cases — DOCUMENTED literal interpretations.
# ---------------------------------------------------------------------------


def test_bare_dollar_sign_is_a_literal():
    # Arrange — no identifier after $, treat as literal.
    # Act
    kind, _ = resolve_env_var_ref("$")
    # Assert
    assert kind == "literal"


def test_empty_braces_dollar_brace_is_a_literal():
    # Arrange — ${} has no identifier inside the braces.
    # Act
    kind, _ = resolve_env_var_ref("${}")
    # Assert
    assert kind == "literal"


def test_numeric_positional_dollar_one_is_a_literal():
    # Arrange — POSIX positional params don't apply to env vars;
    # this is almost certainly a copy-pasted token literal.
    # Act
    kind, _ = resolve_env_var_ref("$1")
    # Assert
    assert kind == "literal"


def test_lowercase_dollar_var_is_a_literal():
    # Arrange — ALL_CAPS env-var convention pins the uppercase-only
    # parse; ``$superkey`` is treated as a literal, NOT an env ref.
    # Act
    kind, _ = resolve_env_var_ref("$superkey")
    # Assert
    assert kind == "literal"


def test_mixed_case_dollar_brace_is_a_literal():
    # Arrange / Act
    kind, _ = resolve_env_var_ref("${Mixed_CASE}")
    # Assert
    assert kind == "literal"


def test_underscore_leading_env_var_is_a_ref():
    # Arrange — POSIX accepts leading underscore in identifiers.
    # Act
    kind, name = resolve_env_var_ref("$_PRIVATE_KEY")
    # Assert
    assert (kind, name) == ("ref", "_PRIVATE_KEY")


def test_digit_in_env_var_name_after_letter_is_a_ref():
    # Arrange — identifiers may contain digits after the first char.
    # Act
    kind, name = resolve_env_var_ref("$XIAOMI2_API_KEY")
    # Assert
    assert (kind, name) == ("ref", "XIAOMI2_API_KEY")


# ---------------------------------------------------------------------------
# Defensive: non-string input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 0, 42, [], {}, object()])
def test_non_string_input_classifies_as_empty(bad):
    # Arrange / Act — parser stays non-raising; validator surfaces
    # the shape error against the raw block.
    kind, name = resolve_env_var_ref(bad)
    # Assert
    assert (kind, name) == ("empty", None)


# ---------------------------------------------------------------------------
# is_env_var_ref convenience wrapper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$XIAOMI_API_KEY", True),
        ("${DEEPSEEK_API_KEY}", True),
        ("sk-ant-xxx", False),
        ("", False),
        ("$", False),
        ("${}", False),
        ("$1", False),
    ],
)
def test_is_env_var_ref_matches_kind_classification(raw, expected):
    # Arrange / Act
    actual = is_env_var_ref(raw)
    # Assert
    assert actual is expected
