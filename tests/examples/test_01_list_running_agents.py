"""Smoke test for examples/01_list_running_agents.py.

Verifies the example script imports and that the API it depends on
(``cli_pkg._helpers.get_agent_list_data``) still exists with the
expected signature. We don't run the @stx.session entry point here
because that would write output side-effects; importing is enough
to catch interface drift.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "01_list_running_agents.py"


def test_example_file_parses() -> None:
    """Example file is syntactically valid Python."""
    ast.parse(EXAMPLE.read_text())


def test_dependent_api_signature_stable() -> None:
    from scitex_agent_container.cli_pkg._helpers import get_agent_list_data

    sig = inspect.signature(get_agent_list_data)
    assert "capability" in sig.parameters
    assert "machine" in sig.parameters
