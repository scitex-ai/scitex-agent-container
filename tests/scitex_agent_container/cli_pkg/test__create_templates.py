"""Tests for the inline ``sac agents create`` spec templates.

The header assertions encode an operator ruling (2026-08-11, called
universal): a spec is the CONTRACT for an agent that has not started yet,
and the state of a RUNNING agent lives in the database. The header exists
because the moment people get this wrong is the moment they have the spec
file open in front of them — see ADR-0022 §3 and skill leaf
``34_spec-is-a-contract-not-state.md``.

Nothing can enforce the header's presence in a spec on disk (a YAML
comment is not schema), so the enforcement point is the generator: a spec
born with the header never needs a sweep.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.cli_pkg._create_templates import (
    _FULL_TEMPLATE,
    _MINIMAL_TEMPLATE,
    _TEMPLATES,
)

_DESIGN_DOC_LINE = (
    "# THIS IS A DESIGN DOCUMENT — the contract for an agent not yet started."
)
_STATE_LINE = (
    "# The state of a RUNNING agent lives in the database, never in this file."
)


def _render(template: str) -> str:
    """Fill the same placeholder set ``sac agents create`` fills."""
    return template.format(
        name="alpha",
        host="scitex-compute-04",
        home="/home/agent",
        credentials_files="[]",
    )


# ---------------------------------------------------------------------------
# The header is the FIRST thing a spec reader sees
# ---------------------------------------------------------------------------


def test_minimal_template_opens_with_the_design_document_line():
    # Arrange
    # Act
    first_line = _render(_MINIMAL_TEMPLATE).splitlines()[0]
    # Assert
    assert first_line == _DESIGN_DOC_LINE


def test_minimal_template_second_line_sends_state_to_the_database():
    # Arrange
    # Act
    second_line = _render(_MINIMAL_TEMPLATE).splitlines()[1]
    # Assert
    assert second_line == _STATE_LINE


def test_full_template_opens_with_the_design_document_line():
    # Arrange
    # Act
    first_line = _render(_FULL_TEMPLATE).splitlines()[0]
    # Assert
    assert first_line == _DESIGN_DOC_LINE


def test_full_template_second_line_sends_state_to_the_database():
    # Arrange
    # Act
    second_line = _render(_FULL_TEMPLATE).splitlines()[1]
    # Assert
    assert second_line == _STATE_LINE


def test_every_registered_template_carries_the_header():
    # Arrange
    # Act
    missing = [
        key
        for key, tmpl in _TEMPLATES.items()
        if _DESIGN_DOC_LINE not in _render(tmpl)
    ]
    # Assert
    assert missing == []


# ---------------------------------------------------------------------------
# The header must stay a COMMENT — it may not change what the spec means
# ---------------------------------------------------------------------------


def test_minimal_template_still_parses_as_a_v3_agent_spec():
    # Arrange
    # Act
    doc = yaml.safe_load(_render(_MINIMAL_TEMPLATE))
    # Assert
    assert doc["apiVersion"] == "scitex-agent-container/v3"


def test_full_template_still_parses_as_a_v3_agent_spec():
    # Arrange
    # Act
    doc = yaml.safe_load(_render(_FULL_TEMPLATE))
    # Assert
    assert doc["apiVersion"] == "scitex-agent-container/v3"
