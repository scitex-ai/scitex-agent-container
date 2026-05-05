"""Tests for info_cmds.

Coverage:
- F-CS9: `sac agent logs` must not crash on bracketed substrings in log
  output (regression test for rich.errors.MarkupError raised when
  log lines contain hook paths like "[/home/.../hook.sh]").
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.info_cmds import logs


def test_logs_does_not_crash_on_bracketed_paths():
    """Log output containing unbalanced "[...]" tokens (e.g. hook script
    paths) must render without raising rich.errors.MarkupError.

    Before F-CS9, ``console.print(output)`` interpreted bracketed text
    as Rich markup and raised when a closing tag had no matching open.
    """
    bracketed_log = (
        "PreToolUse:Bash hook error: "
        "[/home/ywatanabe/.claude/hooks/pre-tool-use/enforce_ripgrep.sh]: "
        "BLOCKED by enforce_ripgrep.sh\n"
        "Some line with [unclosed and another [/different/path] mixed in.\n"
    )

    runner = CliRunner()
    with patch(
        "scitex_agent_container.cli_pkg.info_cmds.agent_logs",
        return_value=bracketed_log,
    ):
        result = runner.invoke(logs, ["any-agent"])

    assert result.exit_code == 0, (
        f"sac agent logs crashed on bracketed text. "
        f"exit_code={result.exit_code} exception={result.exception!r} "
        f"output={result.output!r}"
    )
    # Content must be preserved verbatim (markup disabled).
    assert "/enforce_ripgrep.sh" in result.output


def test_logs_empty_output_renders_placeholder():
    runner = CliRunner()
    with patch(
        "scitex_agent_container.cli_pkg.info_cmds.agent_logs",
        return_value="",
    ):
        result = runner.invoke(logs, ["any-agent"])
    assert result.exit_code == 0
    assert "No log output captured" in result.output


def test_logs_json_mode_returns_lines_array():
    runner = CliRunner()
    payload = "line one\nline two with [brackets]\nline three\n"
    with patch(
        "scitex_agent_container.cli_pkg.info_cmds.agent_logs",
        return_value=payload,
    ):
        result = runner.invoke(logs, ["any-agent", "--json"])
    assert result.exit_code == 0
    import json

    body = json.loads(result.output)
    assert body["name"] == "any-agent"
    assert body["lines"] == [
        "line one",
        "line two with [brackets]",
        "line three",
    ]
