"""Tests for the ``spec.a2a.host`` text editor.

The sweep touches hand-maintained spec files, so these pin what a bulk rewrite
gets wrong: everything not intended stays byte-identical, a re-run does not
duplicate the key, the two decoy keys in every real spec are not mistaken for
the target, and an unrecognised shape is REFUSED with a reason rather than
guessed at.

The second half is the ZERO-BEHAVIOUR-CHANGE proof. The migration's entire
justification is that the value written is the value the code would have used
anyway, so a migrated spec resolves to the same host through the same readers.
That is a claim about OTHER modules, so it is asserted against them rather than
restated in a docstring. Every reader of ``spec.a2a.host`` resolves it one of
two ways:

  1. through :func:`parse_a2a` into ``A2ASpec.host`` — covered by calling it;
  2. by ``a2a_block.get("host", "127.0.0.1")`` on the raw YAML dict —
     ``runtimes/a2a_sidecar.py:254`` (the BIND: it becomes ``a2a serve
     --host``), ``_lifecycle/health.py:110`` and ``cli_pkg/a2a_group.py:163``
     (probe URLs). All three are the same expression, exercised here against
     the real before/after documents.

STX-NM002: no mocks, no monkeypatch — pure string in, value out.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.config._a2a_host_line import (
    REFUSED_ALREADY_DECLARED,
    REFUSED_EMPTY_A2A,
    REFUSED_INLINE_A2A,
    REFUSED_NO_A2A_BLOCK,
    REFUSED_NO_PORT,
    insert_a2a_host,
)
from scitex_agent_container.config._parsers._a2a import parse_a2a
from scitex_agent_container.config._types import A2ASpec

# The real shape of the one spec this migration changes, decoys included.
_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  host: ywata-note-win
  runtime: tui
  a2a:
    port: auto

  startup_commands:
    - command: echo hi
  comms:
    peers:
      a2a:
        listen: true
"""


def test_the_host_is_inserted_after_port_at_the_child_indent() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert — (port, host) is the order the already-declaring specs use.
    assert "  a2a:\n    port: auto\n    host: 127.0.0.1\n" in edit.text


def test_an_insertion_reports_changed() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.changed is True


def test_a_successful_edit_carries_no_refusal_reason() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.reason is None


def test_every_other_line_survives_byte_identical() -> None:
    # Arrange — the whole point of a line edit over a load+dump cycle.
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert [ln for ln in edit.text.splitlines() if "host: 127.0.0.1" not in ln] == (
        text.splitlines()
    )


def test_exactly_one_line_is_added() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert len(edit.text.splitlines()) == len(text.splitlines()) + 1


def test_the_spec_level_host_is_not_treated_as_the_bind_address() -> None:
    # Arrange — `spec.host` is the MACHINE placement and appears EARLIER in
    # the file than the a2a block. A bare `^\\s*host:` anchor hits it first and
    # would report every spec as already declared, migrating nothing.
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert "  host: ywata-note-win\n" in edit.text


def test_the_machine_placement_is_not_the_one_that_changed() -> None:
    # Arrange — pin that the value written went into the a2a block, so a
    # regression that rewrote spec.host cannot pass the test above.
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.text.count("host:") == text.count("host:") + 1


def test_the_nested_comms_a2a_is_left_alone() -> None:
    # Arrange — `spec.comms.peers.a2a` is a different key at a deeper indent.
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert "      a2a:\n        listen: true\n" in edit.text


def test_rerunning_does_not_duplicate_the_key() -> None:
    # Arrange — idempotence; the sweep must be safe to re-run.
    once = insert_a2a_host(_SPEC).text
    # Act
    twice = insert_a2a_host(once)
    # Assert
    assert twice.text.count("host: 127.0.0.1") == 1


def test_an_already_declaring_spec_is_returned_byte_identical() -> None:
    # Arrange — 101 of 102 fleet specs are in this state.
    once = insert_a2a_host(_SPEC).text
    # Act
    twice = insert_a2a_host(once)
    # Assert
    assert twice.text == once


def test_an_already_declaring_spec_says_so() -> None:
    # Arrange — the reason is what separates 101 benign no-ops from a spec
    # whose shape was not recognised.
    once = insert_a2a_host(_SPEC).text
    # Act
    twice = insert_a2a_host(once)
    # Assert
    assert twice.reason == REFUSED_ALREADY_DECLARED


def test_a_spec_without_an_a2a_block_is_refused() -> None:
    # Arrange
    text = "spec:\n  runtime: tui\n  host: ywata-note-win\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.reason == REFUSED_NO_A2A_BLOCK


def test_a_refused_spec_is_returned_untouched() -> None:
    # Arrange — refusal must never be a partial write.
    text = "spec:\n  runtime: tui\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.text == text


def test_an_inline_a2a_mapping_is_refused() -> None:
    # Arrange — a flow mapping is a shape we have not seen; do not guess.
    text = "spec:\n  a2a: {port: auto}\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.reason == REFUSED_INLINE_A2A


def test_an_empty_a2a_block_is_refused() -> None:
    # Arrange
    text = "spec:\n  a2a:\n  workdir: /tmp\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.reason == REFUSED_EMPTY_A2A


def test_an_a2a_block_without_a_port_line_is_refused() -> None:
    # Arrange — no anchor to insert after.
    text = "spec:\n  a2a:\n    handler: echo\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.reason == REFUSED_NO_PORT


def test_crlf_line_endings_are_preserved() -> None:
    # Arrange — a spec edited on Windows must not gain a lone LF.
    text = "spec:\r\n  a2a:\r\n    port: auto\r\n  workdir: /tmp\r\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert "\r\n    host: 127.0.0.1\r\n" in edit.text


def test_an_unterminated_final_line_gains_a_terminator() -> None:
    # Arrange — the real target spec has no trailing newline. Without the
    # repair the two keys would fuse onto one line.
    text = "spec:\n  a2a:\n    port: auto"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert edit.text == "spec:\n  a2a:\n    port: auto\n    host: 127.0.0.1\n"


def test_a_deeper_indent_style_is_matched_not_assumed() -> None:
    # Arrange — 4-space specs must not get a 2-space insertion.
    text = "spec:\n    a2a:\n        port: auto\n"
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert "\n        host: 127.0.0.1\n" in edit.text


def test_the_written_value_is_the_code_default() -> None:
    # Arrange — the whole zero-behaviour-change claim rests on this.
    text = _SPEC
    # Act
    edit = insert_a2a_host(text)
    # Assert
    assert f"host: {DEFAULT_A2A_HOST}\n" in edit.text


# ---------------------------------------------------------------------------
# Zero-behaviour-change proof — the effective bind, before vs after.
# ---------------------------------------------------------------------------

# The a2a block of the one fleet spec that omits the key, verbatim
# (`~/.scitex/agent-container/agents/scitex-todo/spec.yaml`, port: auto).
_BEFORE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  host: ywata-note-win
  runtime: tui
  a2a:
    port: auto
"""


def _raw_reader_host(text: str) -> str:
    """Exactly what the sidecar, the health probe and `a2a doctor` compute."""
    a2a = (yaml.safe_load(text).get("spec") or {}).get("a2a") or {}
    return str(a2a.get("host", DEFAULT_A2A_HOST))


def test_the_bind_path_resolves_to_the_same_host_before_and_after() -> None:
    # Arrange — this is the expression at a2a_sidecar.py:254, which becomes
    # the `--host` argv of the `a2a serve` process. THE bind.
    after = insert_a2a_host(_BEFORE).text
    # Act
    before_host, after_host = _raw_reader_host(_BEFORE), _raw_reader_host(after)
    # Assert
    assert before_host == after_host


def test_the_bind_host_is_byte_identical_to_the_documented_default() -> None:
    # Arrange
    after = insert_a2a_host(_BEFORE).text
    # Act
    resolved = _raw_reader_host(after)
    # Assert
    assert resolved.encode() == b"127.0.0.1"


def test_the_parser_yields_an_equal_a2a_spec_before_and_after() -> None:
    # Arrange — the config path: parse_a2a -> A2ASpec.
    after = insert_a2a_host(_BEFORE).text
    # Act
    before_spec = parse_a2a(yaml.safe_load(_BEFORE)["spec"])
    after_spec = parse_a2a(yaml.safe_load(after)["spec"])
    # Assert
    assert before_spec == after_spec


def test_the_port_is_untouched_by_the_host_edit() -> None:
    # Arrange — an edit that silently altered the port would still satisfy a
    # host-only comparison.
    after = insert_a2a_host(_BEFORE).text
    # Act
    parsed = parse_a2a(yaml.safe_load(after)["spec"])
    # Assert
    assert parsed.port == "auto"


def test_the_dataclass_default_agrees_with_the_written_value() -> None:
    # Arrange — `A2ASpec.host` still holds its own "127.0.0.1" literal because
    # config/_types.py is over the repo line cap and cannot be edited. Pin the
    # agreement so a future drift breaks here instead of silently making the
    # migration a real behaviour change.
    written = DEFAULT_A2A_HOST
    # Act
    dataclass_default = A2ASpec().host
    # Assert
    assert dataclass_default == written


def test_the_parser_default_agrees_with_the_written_value() -> None:
    # Arrange — parse_a2a carries a second literal, at _parsers/_a2a.py:24.
    written = DEFAULT_A2A_HOST
    # Act
    parser_default = parse_a2a({}).host
    # Assert
    assert parser_default == written


def test_nothing_but_the_host_key_differs_in_the_parsed_document() -> None:
    # Arrange — a semantic diff, not a textual one: proves the line edit added
    # exactly one key and disturbed nothing else in the document.
    after = insert_a2a_host(_BEFORE).text
    before_doc, after_doc = yaml.safe_load(_BEFORE), yaml.safe_load(after)
    # Act
    after_doc["spec"]["a2a"].pop("host")
    # Assert
    assert after_doc == before_doc
