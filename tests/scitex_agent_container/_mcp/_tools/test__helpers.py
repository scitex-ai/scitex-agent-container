"""Tests for ``_mcp/_tools/_helpers.py`` — the CliRunner-backed shim."""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

from scitex_agent_container._mcp._tools._helpers import (  # noqa: E402
    invoke_cli_json,
    invoke_cli_text,
)


@pytest.fixture
def help_text_result() -> dict:
    return invoke_cli_text(["--help"])


@pytest.fixture
def list_tools_json_result() -> dict:
    return invoke_cli_json(["mcp", "list-tools", "--json"])


@pytest.fixture
def help_via_json_helper_result() -> dict:
    return invoke_cli_json(["--help"])


def test_invoke_cli_text_returns_zero_exit_code_for_help(
    help_text_result: dict,
) -> None:
    # Arrange
    result = help_text_result
    # Act
    exit_code = result["exit_code"]
    # Assert
    assert exit_code == 0


def test_invoke_cli_text_help_stdout_contains_usage_banner(
    help_text_result: dict,
) -> None:
    # Arrange
    result = help_text_result
    # Act
    stdout = result["stdout"]
    # Assert
    assert "Usage:" in stdout


def test_invoke_cli_json_returns_zero_exit_code_for_list_tools(
    list_tools_json_result: dict,
) -> None:
    # Arrange
    result = list_tools_json_result
    # Act
    exit_code = result["exit_code"]
    # Assert
    assert exit_code == 0


def test_invoke_cli_json_parses_list_tools_payload_into_dict(
    list_tools_json_result: dict,
) -> None:
    # Arrange
    result = list_tools_json_result
    # Act
    data = result["data"]
    # Assert
    assert isinstance(data, dict)


def test_invoke_cli_json_list_tools_payload_contains_tools_key(
    list_tools_json_result: dict,
) -> None:
    # Arrange
    result = list_tools_json_result
    # Act
    data = result["data"]
    # Assert
    assert "tools" in data


def test_invoke_cli_json_leaves_data_none_for_non_json_stdout(
    help_via_json_helper_result: dict,
) -> None:
    # Arrange
    result = help_via_json_helper_result
    # Act
    data = result["data"]
    # Assert
    assert data is None
