"""``spec.required_claude_hooks`` — parsing, and the allowlist that makes it reachable.

The allowlist tests exist because of a measured failure mode, not a hypothesis:
``to_home_layers`` gained a dataclass field and a parser but NOT an entry in
``_KNOWN_SPEC_KEYS``, so every spec declaring it failed to load with "Unknown
spec field" and the whole declaration mechanism was unreachable. The same three
places must be wired for this field, so the same three are asserted.

PA-306 no-mocks: the real ``validate_raw`` and the real parser.
STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.config._parsers._declarations import (
    parse_required_claude_hooks,
)
from scitex_agent_container.config._validation import validate_raw

from .._helpers.explicit_spec import explicit_doc

_FLOOR = {"pre-tool-use": ["enforce_git_dash_C.sh"]}


def _errors_for(spec_overrides: dict) -> "list[str]":
    doc = explicit_doc({"host": "${HOSTNAME}", **spec_overrides})
    return validate_raw(doc, "spec.yaml")


def _refusal_message(value) -> str:
    """The parser's complaint, or ``""`` when it accepted the value.

    One returned string keeps each test to a single assertion (STX-TQ007)
    while still catching only ``ValueError`` — any other exception propagates
    and fails the test loudly rather than reading as "refused".
    """
    try:
        parse_required_claude_hooks(value)
    except ValueError as exc:
        return str(exc)
    return ""


class TestTheFieldIsReachable:
    def test_required_claude_hooks_is_an_accepted_spec_field(self):
        # Arrange
        # Act
        errors = _errors_for({"required_claude_hooks": _FLOOR})
        # Assert
        assert [e for e in errors if "required_claude_hooks" in e] == []

    def test_omitting_required_claude_hooks_is_not_an_error(self):
        # Arrange — OPTIONAL by design: absent from the explicit-required map,
        # so the 100+ specs that never mention it keep loading and starting.
        # Act
        errors = _errors_for({})
        # Assert
        assert [e for e in errors if "required_claude_hooks" in e] == []


class TestTheParserNormalises:
    def test_absent_declaration_is_none(self):
        # Arrange
        # Act
        parsed = parse_required_claude_hooks(None)
        # Assert
        assert parsed is None

    def test_empty_declaration_stays_distinguishable_from_absence(self):
        # Arrange — `{}` is a spec deliberately requiring no hooks. Collapsing
        # it into None would erase a statement its author made on purpose.
        # Act
        parsed = parse_required_claude_hooks({})
        # Assert
        assert parsed == {}

    def test_a_bare_scalar_becomes_a_one_element_list(self):
        # Arrange — a single required hook is the common case and writing it as
        # a scalar is the obvious thing to do in YAML.
        # Act
        parsed = parse_required_claude_hooks({"pre-tool-use": "one.sh"})
        # Assert
        assert parsed == {"pre-tool-use": ["one.sh"]}

    def test_duplicate_names_collapse(self):
        # Arrange
        # Act
        parsed = parse_required_claude_hooks({"stop": ["a.sh", "a.sh", "b.sh"]})
        # Assert
        assert parsed == {"stop": ["a.sh", "b.sh"]}


class TestAMalformedDeclarationRaises:
    """It must never degrade to "absent": a broken declaration that reads as no
    declaration means the spec silently enforces nothing while its author
    believes it stated a floor — the exact surprise the field exists to remove."""

    def test_a_list_is_refused(self):
        # Arrange — the shape someone reaches for first; it loses the event dir.
        value = ["enforce_git_dash_C.sh"]
        # Act
        refused = _refusal_message(value) != ""
        # Assert
        assert refused is True

    def test_a_scalar_is_refused(self):
        # Arrange
        value = "enforce_git_dash_C.sh"
        # Act
        refused = _refusal_message(value) != ""
        # Assert
        assert refused is True

    def test_a_non_list_entry_is_refused(self):
        # Arrange
        value = {"pre-tool-use": {"nested": "wrong"}}
        # Act
        refused = _refusal_message(value) != ""
        # Assert
        assert refused is True

    def test_the_error_shows_the_expected_shape(self):
        # Arrange — a refusal that does not show the correct spelling makes the
        # author guess, and the next guess is another malformed declaration.
        # Act
        try:
            parse_required_claude_hooks(["x.sh"])
            message = ""
        except ValueError as exc:
            message = str(exc)
        # Assert
        assert "required_claude_hooks:" in message
