"""The arrival matcher must survive what the TUI does to the token.

The negative these tests pin is the one that already happened: a search that
returns "not delivered" about a message sitting on the peer's screen.
"""

from __future__ import annotations

from scitex_agent_container._delivery._token import (
    DELIVERY_TOKEN_BYTES,
    flatten_pane,
    format_payload,
    make_token,
    pane_contains_token,
)

#: A token split across a soft wrap with the composer's border drawn through the
#: seam — byte-for-byte the shape that defeated a naive substring search.
_WRAPPED = (
    "  earlier turn\n────────\n❯ [sac-deliver:ab12cd │\n│ 34ef56] hello │\n────────\n"
)


def test_token_survives_wrapped_border_split():
    # Arrange
    pane = _WRAPPED
    # Act
    found = pane_contains_token(pane, "ab12cd34ef56")
    # Assert
    assert found is True


def test_raw_substring_search_would_have_failed():
    # Arrange
    pane = _WRAPPED
    # Act
    naive = "ab12cd34ef56" in pane
    # Assert
    assert naive is False


def test_single_occurrence_counts_as_delivered():
    # Arrange
    pane = "❯ [sac-deliver:deadbeef0123] hello\n"
    # Act
    found = pane_contains_token(pane, "deadbeef0123")
    # Assert
    assert found is True


def test_unreadable_pane_renders_none_not_false():
    # Arrange
    pane = None
    # Act
    found = pane_contains_token(pane, "deadbeef0123")
    # Assert
    assert found is None


def test_absent_token_renders_false_cleanly():
    # Arrange
    pane = "❯ nothing of interest here\n"
    # Act
    found = pane_contains_token(pane, "deadbeef0123")
    # Assert
    assert found is False


def test_flatten_strips_every_border_artefact():
    # Arrange
    pane = "│ AB 12 │\n│ cd-34 │\n"
    # Act
    flat = flatten_pane(pane)
    # Assert
    assert flat == "ab12cd34"


def test_payload_places_token_before_message():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("hello there", token)
    # Assert
    assert payload == "[sac-deliver:deadbeef0123] hello there"


def test_generated_token_is_twelve_hex():
    # Arrange
    expected_length = DELIVERY_TOKEN_BYTES * 2
    # Act
    token = make_token()
    # Assert
    assert len(token) == expected_length


def test_two_tokens_are_not_equal():
    # Arrange
    first = make_token()
    # Act
    second = make_token()
    # Assert
    assert first != second
