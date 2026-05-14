"""Tests for ``_mcp/_tools/_helpers.py`` — the CliRunner-backed shim."""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

from scitex_agent_container._mcp._tools._helpers import (  # noqa: E402
    invoke_cli_json,
    invoke_cli_text,
)


def test_invoke_cli_text_returns_exit_code_and_stdout():
    """`sac --help` prints help and exits 0."""
    result = invoke_cli_text(["--help"])
    assert result["exit_code"] == 0
    assert "Usage:" in result["stdout"]


def test_invoke_cli_json_parses_when_output_is_json():
    """`sac mcp list-tools --json` returns a JSON object."""
    result = invoke_cli_json(["mcp", "list-tools", "--json"])
    assert result["exit_code"] == 0
    assert result["data"] is not None
    assert isinstance(result["data"], dict)
    assert "tools" in result["data"]


def test_invoke_cli_json_data_none_for_non_json_output():
    """Non-JSON stdout leaves ``data`` None and the raw text in ``stdout``."""
    result = invoke_cli_json(["--help"])
    assert result["exit_code"] == 0
    assert result["data"] is None
    assert "Usage:" in result["stdout"]
