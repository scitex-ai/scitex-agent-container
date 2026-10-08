"""Pytest wrapper around the exit-code hook's embedded ``--self-test`` mode.

``src/scitex_agent_container/_baseline_assets/exit_code_hooks/
deny_swallowed_exit_code.sh`` carries a ``--self-test`` mode that exercises
its refuse/allow contract with canned (command, want_rc) cases and reports
``pass=N fail=M``. CI runs it via this pytest so a regression to the script
LOGIC or the TEST CONTRACT itself surfaces on PR -- without needing the live
Claude Code PreToolUse matcher chain to actually fire.

A second layer below pins the operator-facing guarantees directly: piped
commands require pipefail, provable swallows are refused, and legitimate
shapes (quoted pipes, real ``||`` handlers, PIPESTATUS checks) keep working.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "exit_code_hooks"
    / "deny_swallowed_exit_code.sh"
)


def _run_self_test() -> subprocess.CompletedProcess[str]:
    assert _HOOK.is_file(), f"hook script missing: {_HOOK}"
    assert _HOOK.stat().st_mode & 0o111, f"hook script not executable: {_HOOK}"
    return subprocess.run(
        ["bash", str(_HOOK), "--self-test"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _run_hook(command: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_self_test_suite_is_green() -> None:
    # Arrange + Act
    result = _run_self_test()
    # Assert
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fail=0" in result.stdout


def test_refuses_bare_pipeline() -> None:
    # Arrange
    command = "pytest tests/ -q | tail -5"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 2
    assert "pipefail" in result.stderr


def test_refuses_or_true_swallow() -> None:
    # Arrange
    command = "grep foo file || true"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 2
    assert "|| true" in result.stderr


def test_refuses_trailing_semicolon_true() -> None:
    # Arrange
    command = "run_tests; true"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 2


def test_allows_pipeline_under_pipefail() -> None:
    # Arrange
    command = "set -o pipefail; pytest -q | tail -5"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_allows_real_or_handler() -> None:
    # Arrange
    command = "grep foo file || echo 'not found'"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_allows_quoted_pipe_as_data() -> None:
    # Arrange
    command = 'grep -E "a|b" file'
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_ignores_non_bash_tool() -> None:
    # Arrange
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"command": "x || true"}})
    # Act
    result = subprocess.run(
        ["bash", str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    # Assert
    assert result.returncode == 0


def test_fails_open_on_unparseable_payload() -> None:
    # Arrange — a broken guard must not wedge the Bash tool
    # Act
    result = subprocess.run(
        ["bash", str(_HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    # Assert
    assert result.returncode == 0
