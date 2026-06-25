"""Tests for the tmux named-key vocabulary (``_runners._tmux._keys``).

``sac agents send --key/--keys`` validates every key name against this
module before routing it to ``tmux send-keys``. The contract:

  * named tmux keys (Enter, Up, BTab, …) pass through verbatim;
  * the ``ESC`` alias canonicalises to ``Escape``;
  * ``C-``/``M-``/``S-`` modifier combos pass through;
  * single printable literal chars (digits / letters / punctuation)
    pass through as raw input;
  * everything else is rejected fail-loud with the valid set listed.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._tmux._keys import (
    NAMED_KEYS,
    UnknownKeyError,
    parse_key_sequence,
    validate_key,
    validate_keys,
)

# ---------------------------------------------------------------------------
# validate_key — named keys
# ---------------------------------------------------------------------------


class TestValidateKeyNamed:
    """Known tmux keyword names pass through verbatim."""

    def test_enter_passes_through(self) -> None:
        # Arrange
        token = "Enter"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "Enter"

    def test_up_arrow_passes_through(self) -> None:
        # Arrange
        token = "Up"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "Up"

    def test_btab_passes_through(self) -> None:
        # Arrange
        token = "BTab"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "BTab"


class TestValidateKeyAlias:
    """The ESC alias canonicalises to the tmux ``Escape`` keyword."""

    def test_esc_maps_to_escape(self) -> None:
        # Arrange
        token = "ESC"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "Escape"


# ---------------------------------------------------------------------------
# validate_key — modifier combos
# ---------------------------------------------------------------------------


class TestValidateKeyModifier:
    """``C-``/``M-``/``S-`` combos pass through verbatim."""

    def test_ctrl_c_passes_through(self) -> None:
        # Arrange
        token = "C-c"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "C-c"

    def test_meta_x_passes_through(self) -> None:
        # Arrange
        token = "M-x"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "M-x"

    def test_ctrl_named_key_passes_through(self) -> None:
        # Arrange
        token = "C-Left"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "C-Left"


# ---------------------------------------------------------------------------
# validate_key — literal characters
# ---------------------------------------------------------------------------


class TestValidateKeyLiteral:
    """Single printable chars pass through as raw input."""

    def test_digit_passes_through(self) -> None:
        # Arrange
        token = "1"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "1"

    def test_letter_passes_through(self) -> None:
        # Arrange
        token = "y"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "y"

    def test_punctuation_passes_through(self) -> None:
        # Arrange
        token = "/"
        # Act
        result = validate_key(token)
        # Assert
        assert result == "/"


# ---------------------------------------------------------------------------
# validate_key — rejection
# ---------------------------------------------------------------------------


class TestValidateKeyRejection:
    """Unknown names raise fail-loud with the valid set listed."""

    def test_unknown_word_raises(self) -> None:
        # Arrange
        token = "Retrun"
        # Act
        call = lambda: validate_key(token)
        # Assert
        with pytest.raises(UnknownKeyError, match="unsupported key"):
            call()

    def test_error_lists_a_named_key(self) -> None:
        # Arrange
        token = "Retrun"
        # Act
        call = lambda: validate_key(token)
        # Assert
        with pytest.raises(UnknownKeyError, match="Enter"):
            call()

    def test_empty_string_raises(self) -> None:
        # Arrange
        token = ""
        # Act
        call = lambda: validate_key(token)
        # Assert
        with pytest.raises(UnknownKeyError):
            call()


# ---------------------------------------------------------------------------
# validate_keys — sequences
# ---------------------------------------------------------------------------


class TestValidateKeysSequence:
    """A whole sequence validates in order; first bad token fails loud."""

    def test_valid_sequence_returns_all(self) -> None:
        # Arrange
        tokens = ["Up", "Up", "Enter"]
        # Act
        result = validate_keys(tokens)
        # Assert
        assert result == ["Up", "Up", "Enter"]

    def test_alias_in_sequence_canonicalised(self) -> None:
        # Arrange
        tokens = ["ESC", "Enter"]
        # Act
        result = validate_keys(tokens)
        # Assert
        assert result == ["Escape", "Enter"]

    def test_first_bad_token_raises(self) -> None:
        # Arrange
        tokens = ["Up", "Nope", "Enter"]
        # Act
        call = lambda: validate_keys(tokens)
        # Assert
        with pytest.raises(UnknownKeyError):
            call()

    def test_empty_sequence_raises_value_error(self) -> None:
        # Arrange
        tokens: list[str] = []
        # Act
        call = lambda: validate_keys(tokens)
        # Assert
        with pytest.raises(ValueError, match="empty sequence"):
            call()


# ---------------------------------------------------------------------------
# parse_key_sequence
# ---------------------------------------------------------------------------


class TestParseKeySequence:
    """Whitespace-separated spec splits into tokens."""

    def test_splits_on_whitespace(self) -> None:
        # Arrange
        spec = "Up Up Enter"
        # Act
        result = parse_key_sequence(spec)
        # Assert
        assert result == ["Up", "Up", "Enter"]

    def test_blank_yields_empty_list(self) -> None:
        # Arrange
        spec = "   "
        # Act
        result = parse_key_sequence(spec)
        # Assert
        assert result == []


# ---------------------------------------------------------------------------
# NAMED_KEYS export
# ---------------------------------------------------------------------------


class TestNamedKeysExport:
    """The public vocabulary carries the documented names."""

    def test_includes_arrow_keys(self) -> None:
        # Arrange
        arrows = {"Up", "Down", "Left", "Right"}
        # Act
        present = arrows <= NAMED_KEYS
        # Assert
        assert present is True

    def test_includes_esc_alias(self) -> None:
        # Arrange
        alias = "ESC"
        # Act
        present = alias in NAMED_KEYS
        # Assert
        assert present is True


# EOF
