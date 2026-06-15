"""Tests for ``spec.claude.effort`` parsing + validation.

Mirrors the model-field plumbing (see ``test__validation.py`` and
``_parsers/test__claude.py``). The bundled claude binary (2.1.150 in the
sac-base SIF) supports ``--effort <level>`` with values
``low / medium / high / xhigh / max``; the SDK uses settings.json
``effortLevel``. Operator directive 2026-06-15: surface this knob fleet-
wide so every agent can run at effort=max.

The parser plumbs the YAML field through to ``ClaudeSpec.effort``;
the validator enforces the documented value set. Empty / missing means
"no override" — the runtime uses the claude binary's own default.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007). Error
tests use ``pytest.raises(match=...)`` or list-membership checks per
fleet doctrine.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._claude import parse_claude
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.config._validation import validate_raw

# ---------------------------------------------------------------------------
# ClaudeSpec default
# ---------------------------------------------------------------------------


def test_claude_spec_default_effort_is_empty_string():
    # Arrange / Act
    spec = ClaudeSpec()
    # Assert
    assert spec.effort == ""


# ---------------------------------------------------------------------------
# parse_claude — pass-through
# ---------------------------------------------------------------------------


def test_parse_claude_missing_block_yields_empty_effort():
    # Arrange
    raw: dict = {}
    # Act
    result = parse_claude(raw)
    # Assert
    assert result.effort == ""


def test_parse_claude_missing_effort_key_yields_empty_effort():
    # Arrange
    raw = {"claude": {"model": "opus"}}
    # Act
    result = parse_claude(raw)
    # Assert
    assert result.effort == ""


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_parse_claude_accepts_documented_levels(level):
    # Arrange
    raw = {"claude": {"effort": level}}
    # Act
    result = parse_claude(raw)
    # Assert
    assert result.effort == level


def test_parse_claude_coerces_non_string_to_empty():
    # Arrange — defensive: unvalidated path must not crash the runner.
    raw = {"claude": {"effort": 42}}
    # Act
    result = parse_claude(raw)
    # Assert
    assert result.effort == ""


def test_parse_claude_explicit_none_yields_empty_effort():
    # Arrange
    raw = {"claude": {"effort": None}}
    # Act
    result = parse_claude(raw)
    # Assert
    assert result.effort == ""


# ---------------------------------------------------------------------------
# validate_raw — accepts documented values
# ---------------------------------------------------------------------------

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "apptainer"},
}


def _spec_with_effort(effort):
    return {
        **_BASE,
        "spec": {**_BASE["spec"], "claude": {"effort": effort}},
    }


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_validate_accepts_documented_effort_levels(level):
    # Arrange
    raw = _spec_with_effort(level)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.claude.effort" in e]


def test_validate_accepts_missing_effort():
    # Arrange
    raw = _BASE
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.claude.effort" in e]


def test_validate_accepts_empty_effort():
    # Arrange
    raw = _spec_with_effort("")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.claude.effort" in e]


# ---------------------------------------------------------------------------
# validate_raw — rejects unknown / mistyped values
# ---------------------------------------------------------------------------

_INVALID_EFFORTS = [
    "extreme",  # not in the documented set
    "MAX",  # claude expects lowercase
    "ultra",
    "0",
    "highest",
]


@pytest.mark.parametrize("level", _INVALID_EFFORTS)
def test_validate_rejects_unknown_effort_value(level):
    # Arrange
    raw = _spec_with_effort(level)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.effort" in e]


@pytest.mark.parametrize("level", _INVALID_EFFORTS)
def test_validate_rejection_echoes_offending_value(level):
    # Arrange
    raw = _spec_with_effort(level)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.effort" in e]
    # Assert
    assert level in bad[0]


@pytest.mark.parametrize("level", _INVALID_EFFORTS)
def test_validate_rejection_lists_canonical_levels(level):
    # Arrange
    raw = _spec_with_effort(level)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.effort" in e]
    # Assert
    assert "max" in bad[0] and "low" in bad[0]


def test_validate_rejects_non_string_effort():
    # Arrange
    raw = _spec_with_effort(42)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.effort" in e]


def test_validate_non_string_effort_message_calls_out_type():
    # Arrange
    raw = _spec_with_effort(42)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.effort" in e]
    # Assert
    assert "string" in bad[0].lower()
