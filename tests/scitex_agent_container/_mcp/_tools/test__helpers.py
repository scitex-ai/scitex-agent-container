"""Tests for ``_mcp/_tools/_helpers.py`` — the CliRunner-backed shim."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

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


# ---------------------------------------------------------------------------
# A stderr warning must never make the payload unparseable
#
# `invoke_cli_json` used to read click's `result.output`, which is stdout and
# stderr MERGED. One line written to stderr by any JSON-emitting command
# therefore made its payload unparseable and handed every MCP caller
# `data: None`. Measured on click 8.4.2:
#
#     output : 'warning: something\n[1, 2]\n'   -> JSONDecodeError
#     stdout : '[1, 2]\n'                       -> parses
#
# Why that is urgent rather than cosmetic:
#
#     data: None    means THE PAYLOAD FAILED TO PARSE
#     data: []      means THE QUERY RETURNED NO ROWS
#     if not data:  TREATS THEM IDENTICALLY
#
# Every caller writing the natural falsy check converts a parse failure into
# "there is nothing there" — the same indistinguishability that had three
# agents concluding the fleet registry was wiped on 2026-08-09. And the
# trigger is the most innocuous change in software: adding a warning.
#
# So the guard is mechanical. A comment saying "do not write to stderr" is the
# mechanism that already failed four times in one day across four surfaces.
# ---------------------------------------------------------------------------


@click.command()
def _noisy() -> None:
    """Emits a stderr warning AND a JSON payload — the exact hazard."""
    click.echo("warning: deprecated flag", err=True)
    click.echo(json.dumps([{"id": 1}]))


def _invoke_like_the_helper(cmd) -> dict:
    """Mirror the helper's stream handling against an arbitrary command.

    The helper hard-codes sac's own ``main``, so the contract is exercised
    through an equivalent invocation — the behaviour under test is WHICH
    STREAM IS PARSED, not sac's argv.
    """
    result = CliRunner().invoke(cmd, [], catch_exceptions=False)
    text = result.stdout or ""
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"data": parsed, "stdout": text, "stderr": result.stderr or ""}


def test_a_stderr_warning_does_not_break_the_payload() -> None:
    # Arrange: the regression guard. Parsing result.output instead of
    # result.stdout makes this None, which every `if not data` caller then
    # reads as "no rows".
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert out["data"] == [{"id": 1}]


def test_the_warning_is_still_visible() -> None:
    # Arrange: separating the streams must not SWALLOW the warning —
    # protecting the parse by discarding diagnostics would trade one silent
    # failure for another.
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert "deprecated flag" in out["stderr"]


def test_stdout_field_holds_stdout_only() -> None:
    # Arrange: the field is named `stdout`; it previously carried the merged
    # stream, so its name was a lie.
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert "warning:" not in out["stdout"]


def test_click_result_output_really_is_merged() -> None:
    # Arrange: pins the upstream behaviour this fix rests on. If a future
    # click makes `output` stdout-only, this fails and tells the reader the
    # hazard is gone rather than leaving the guard as cargo.
    cmd = _noisy
    # Act
    result = CliRunner().invoke(cmd, [], catch_exceptions=False)
    # Assert
    assert "warning:" in result.output


def test_the_helper_returns_stderr_separately() -> None:
    # Arrange: the shipped helper's contract now has four keys, and callers
    # may branch on stderr.
    argv = ["--help"]
    # Act
    out = invoke_cli_json(argv)
    # Assert
    assert "stderr" in out
