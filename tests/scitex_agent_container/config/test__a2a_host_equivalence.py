"""The zero-behaviour-change proof for the ``spec.a2a.host`` migration.

The migration's entire justification is that the value it writes is the value
the code would have used anyway, so a migrated spec resolves to the same host
through the same readers. That is a claim about OTHER modules, so it is
asserted against them here rather than restated in a docstring.

Every reader of ``spec.a2a.host`` in this tree resolves it one of two ways:

  1. through :func:`parse_a2a` into ``A2ASpec.host`` — covered by calling it;
  2. by ``a2a_block.get("host", "127.0.0.1")`` on the raw YAML dict —
     ``runtimes/a2a_sidecar.py:109`` (the BIND: it becomes ``a2a serve
     --host``), ``_lifecycle/health.py:110`` and ``cli_pkg/a2a_group.py:163``
     (probe URLs). All three are the same expression, so it is exercised here
     against the real before/after documents.

STX-NM002: no mocks, no monkeypatch — real parser, real text.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.config._a2a_host_line import insert_a2a_host
from scitex_agent_container.config._parsers._a2a import parse_a2a
from scitex_agent_container.config._types import A2ASpec

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


def _spec_block(text: str) -> dict:
    return (yaml.safe_load(text).get("spec") or {}).get("a2a") or {}


def _raw_reader_host(text: str) -> str:
    """Exactly what the sidecar, the health probe and `a2a doctor` compute."""
    return str(_spec_block(text).get("host", DEFAULT_A2A_HOST))


def test_the_bind_path_resolves_to_the_same_host_before_and_after() -> None:
    # Arrange — this is the expression at a2a_sidecar.py:109, which becomes
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
    assert resolved == "127.0.0.1"


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


def test_a_spec_that_already_declares_it_is_completely_unaffected() -> None:
    # Arrange — 101 of 102 specs; the sweep must be a no-op on every one.
    declared = insert_a2a_host(_BEFORE).text
    # Act
    again = insert_a2a_host(declared)
    # Assert
    assert again.text == declared


def test_nothing_but_the_host_key_differs_in_the_parsed_document() -> None:
    # Arrange — a semantic diff, not a textual one: proves the line edit added
    # exactly one key and disturbed nothing else in the document.
    after = insert_a2a_host(_BEFORE).text
    before_doc, after_doc = yaml.safe_load(_BEFORE), yaml.safe_load(after)
    # Act
    after_doc["spec"]["a2a"].pop("host")
    # Assert
    assert after_doc == before_doc
