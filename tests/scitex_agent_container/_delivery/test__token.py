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


# --- slash commands: the command word must stay the FIRST token -------------


def test_steer_command_keeps_the_command_first():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("/steer stop and turn left", token)
    # Assert
    assert payload.startswith("/steer ")


def test_steer_payload_still_carries_the_token():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("/steer stop and turn left", token)
    # Assert
    assert f"[sac-deliver:{token}]" in payload


def test_queue_command_keeps_the_command_first():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("/queue hello later", token)
    # Assert
    assert payload.startswith("/queue ")


def test_queue_payload_still_carries_the_token():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("/queue hello later", token)
    # Assert
    assert f"[sac-deliver:{token}]" in payload


def test_ordinary_message_still_puts_the_token_first():
    # Arrange
    token = "deadbeef0123"
    # Act
    payload = format_payload("hello there", token)
    # Assert
    assert payload == f"[sac-deliver:{token}] hello there"


def test_bare_slash_is_not_a_command():
    # Arrange — a lone "/" is prose, not a TUI command; token stays first.
    token = "deadbeef0123"
    # Act
    payload = format_payload("/ just a slash", token)
    # Assert
    assert payload == f"[sac-deliver:{token}] / just a slash"


def test_slash_command_token_is_found_after_a_wrap():
    # Arrange — the token now sits LAST on a slash command; prove the matcher
    # still finds it wherever it lands (verification is position-independent).
    token = "ab12cd34ef56"
    payload = format_payload("/steer go", token)
    pane = f"❯ /steer go [sac-deliver:ab12cd │\n│ 34ef56]\n"
    # Act
    found = pane_contains_token(pane, token)
    # Assert
    assert found is True

