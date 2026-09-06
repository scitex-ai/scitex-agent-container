"""Tests for the shared spec line-edit primitives.

The one these exist for is :func:`find_block`. The fleet's specs contain
``spec.host`` (102 of 102) and ``spec.comms.a2a`` (101 of 102) alongside the
``spec.a2a`` an a2a edit means, and both decoys satisfy a bare regex anchor.
Path-matching is what keeps them out, so it is pinned per decoy.

STX-NM002: no mocks, no monkeypatch — pure string in, value out.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

from scitex_agent_container.config._yaml_line_edit import (
    find_block,
    find_key,
    insert_after,
    is_skippable,
    last_content_line,
    parse_key_line,
    split_ending,
)

# The real shape: `host` at spec level (machine placement), an `a2a` block with
# the bind address, and a SECOND `a2a` nested under `comms`.
_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  host: ywata-note-win
  runtime: tui
  a2a:
    port: auto
  comms:
    peers:
      a2a:
        listen: true
"""


def _bodies(text: str) -> "list[str]":
    return [split_ending(raw)[0] for raw in text.splitlines(keepends=True)]


def test_the_a2a_block_is_found_at_its_own_line() -> None:
    # Arrange
    bodies = _bodies(_SPEC)
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert — line 5 (0-indexed) is `  a2a:`, not `  host:` on line 3.
    assert bodies[block.key_line] == "  a2a:"


def test_the_nested_comms_a2a_is_not_mistaken_for_it() -> None:
    # Arrange — the deeper `a2a:` must not be reachable at spec level.
    bodies = _bodies(_SPEC)
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert — the block covers only `port: auto`, not the comms subtree.
    assert bodies[block.start : block.stop] == ["    port: auto"]


def test_the_spec_level_host_is_outside_the_a2a_block() -> None:
    # Arrange — `spec.host` precedes the a2a block in every real spec.
    bodies = _bodies(_SPEC)
    block = find_block(bodies, ("spec", "a2a"))
    # Act
    found = find_key(bodies, block.start, block.stop, "    ", "host")
    # Assert
    assert found is None


def test_a_missing_path_returns_none() -> None:
    # Arrange
    bodies = _bodies("spec:\n  runtime: tui\n")
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert
    assert block is None


def test_an_inline_value_is_reported_rather_than_descended_into() -> None:
    # Arrange — `a2a: {port: auto}` is a shape we do not edit.
    bodies = _bodies("spec:\n  a2a: {port: auto}\n")
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert
    assert block.inline_value == "{port: auto}"


def test_a_comment_between_siblings_does_not_end_the_block() -> None:
    # Arrange — comments are everywhere in these specs; treating one as a
    # terminator would truncate the search before the interesting keys.
    text = "spec:\n  a2a:\n    port: auto\n    # why we pin it\n    handler: echo\n"
    bodies = _bodies(text)
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert
    assert find_key(bodies, block.start, block.stop, "    ", "handler") == 4


def test_a_sibling_key_ends_the_block() -> None:
    # Arrange
    bodies = _bodies("spec:\n  a2a:\n    port: auto\n  workdir: /tmp\n")
    # Act
    block = find_block(bodies, ("spec", "a2a"))
    # Assert
    assert block.stop == 3


def test_a_sequence_item_is_not_read_as_a_key() -> None:
    # Arrange — `- command: …` must not be matched as a mapping key.
    bodies = _bodies("spec:\n  startup_commands:\n    - command: echo hi\n")
    # Act
    found = find_key(bodies, 0, len(bodies), "    ", "command")
    # Assert
    assert found is None


def test_split_ending_preserves_crlf() -> None:
    # Arrange
    raw = "  a2a:\r\n"
    # Act
    _, ending = split_ending(raw)
    # Assert
    assert ending == "\r\n"


def test_split_ending_reports_a_missing_terminator() -> None:
    # Arrange
    raw = "  port: auto"
    # Act
    _, ending = split_ending(raw)
    # Assert
    assert ending == ""


def test_insert_after_reuses_the_anchor_line_ending() -> None:
    # Arrange
    lines = ["spec:\r\n", "  a2a:\r\n", "    port: auto\r\n"]
    # Act
    insert_after(lines, 2, "    ", "host", "127.0.0.1")
    # Assert
    assert lines[3] == "    host: 127.0.0.1\r\n"


def test_an_unterminated_anchor_gains_a_terminator() -> None:
    # Arrange — without this the two keys fuse onto one line.
    lines = ["spec:\n", "  a2a:\n", "    port: auto"]
    # Act
    insert_after(lines, 2, "    ", "host", "127.0.0.1")
    # Assert
    assert "".join(lines).endswith("    port: auto\n    host: 127.0.0.1\n")


# ---------------------------------------------------------------------------
# parse_key_line — the ONE regex a value-reader is allowed to use
# ---------------------------------------------------------------------------


def test_parse_key_line_returns_the_value() -> None:
    # Arrange
    body = "    model: opus[1m]"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed.value == "opus[1m]"


def test_parse_key_line_returns_the_key() -> None:
    # Arrange
    body = "    model: opus[1m]"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed.key == "model"


def test_parse_key_line_returns_the_indent() -> None:
    # Arrange
    body = "    model: opus[1m]"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed.indent == "    "


def test_parse_key_line_reports_a_block_key_as_an_empty_value() -> None:
    # Arrange — "" is what tells a caller this key opens a block.
    body = "  claude:"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed.value == ""


def test_parse_key_line_refuses_a_sequence_item() -> None:
    # Arrange — an unfamiliar shape must become a refusal, not a guess.
    body = "  - command: run"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed is None


def test_parse_key_line_refuses_a_comment() -> None:
    # Arrange
    body = "  # model: opus[1m]"
    # Act
    parsed = parse_key_line(body)
    # Assert
    assert parsed is None


# ---------------------------------------------------------------------------
# last_content_line — where a multi-line replacement must STOP
# ---------------------------------------------------------------------------

_BLOCK = """\
spec:
  provider:
    base_url: http://127.0.0.1:4000
    auth_token_env: TOKEN

  # the account below is pinned deliberately
  account: ''
"""


def test_last_content_line_finds_the_final_child() -> None:
    # Arrange — the block's `stop` is the next SIBLING key, so replacing
    # through it would eat the blank and comment introducing that sibling.
    bodies = _bodies(_BLOCK)
    block = find_block(bodies, ("spec", "provider"))
    # Act
    end = last_content_line(bodies, block.start, block.stop)
    # Assert
    assert bodies[end] == "    auth_token_env: TOKEN"


def test_last_content_line_stops_before_the_next_keys_comment() -> None:
    # Arrange — this is the riskiest span computation in the engines edit.
    bodies = _bodies(_BLOCK)
    block = find_block(bodies, ("spec", "provider"))
    # Act
    end = last_content_line(bodies, block.start, block.stop)
    # Assert
    assert "# the account below" not in "".join(bodies[block.start : end + 1])


def test_last_content_line_is_none_when_the_range_is_all_skippable() -> None:
    # Arrange
    bodies = ["", "  # nothing but a note", "   "]
    # Act
    end = last_content_line(bodies, 0, 3)
    # Assert
    assert end is None


# ---------------------------------------------------------------------------
# is_skippable — "does this line carry structure?", asked once
# ---------------------------------------------------------------------------


def test_a_blank_line_is_skippable() -> None:
    # Arrange
    body = "   "
    # Act
    answer = is_skippable(body)
    # Assert
    assert answer is True


def test_a_comment_line_is_skippable() -> None:
    # Arrange
    body = "  # PINNED 2026-08-14"
    # Act
    answer = is_skippable(body)
    # Assert
    assert answer is True


def test_a_key_line_is_not_skippable() -> None:
    # Arrange
    body = "  claude:"
    # Act
    answer = is_skippable(body)
    # Assert
    assert answer is False
