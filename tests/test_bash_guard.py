"""Tests for the PreToolUse bash guard (todo#424).

Verifies that dangerous find/du patterns are blocked and safe commands pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scitex_agent_container.cli_pkg.hook_cmds import _is_dangerous_bash


# ---------------------------------------------------------------------------
# Unit tests for _is_dangerous_bash
# ---------------------------------------------------------------------------


class TestIsDangerousBash:
    @pytest.mark.parametrize(
        "cmd",
        [
            "find / -name pdflatex 2>/dev/null",
            "find /",
            "find / ",
            "find ~ -name foo",
            "find ~/",
            "find $HOME -name foo",
            "find $HOME/",
            "du -a /",
            "du -a / --max-depth=1",
            "du -a ~",
        ],
    )
    def test_dangerous_commands_blocked(self, cmd):
        assert _is_dangerous_bash(cmd), f"expected {cmd!r} to be blocked"

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -name foo",
            "find ./src -name '*.py'",
            "find /scratch -name pdflatex",
            "find /home/user -name foo",
            "which pdflatex",
            "command -v pdflatex",
            "ls /",
            "ls /usr",
            "grep -r pattern .",
            "module avail texlive",
            "du -sh .",
            "du -h ./logs",
            # Issue text mentioning find / inside --comment arg (false-positive guard)
            'gh issue close 424 --comment "blocked find / in the past"',
            'echo "do not run: find / -name foo"',
        ],
    )
    def test_safe_commands_allowed(self, cmd):
        assert not _is_dangerous_bash(cmd), f"expected {cmd!r} to be allowed"


# ---------------------------------------------------------------------------
# Integration: hook-event pretool blocks via exit code 2
# ---------------------------------------------------------------------------


def _pretool_payload(cmd: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})


def _run_hook_event(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scitex_agent_container.cli_pkg._main", "hook-event", "pretool"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "find / -name pdflatex 2>/dev/null",
        "find ~ -name foo",
        "find $HOME -name bar",
        "du -a /",
    ],
)
def test_hook_event_exits_2_for_dangerous_bash(cmd):
    """hook-event pretool must exit 2 (block) for dangerous commands."""
    result = _run_hook_event(_pretool_payload(cmd))
    assert result.returncode == 2, (
        f"expected exit 2 for {cmd!r}, got {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "BLOCKED" in result.stdout


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -name foo",
        "which pdflatex",
        "ls /",
    ],
)
def test_hook_event_exits_0_for_safe_bash(cmd):
    """hook-event pretool must exit 0 (allow) for safe commands."""
    result = _run_hook_event(_pretool_payload(cmd))
    assert result.returncode == 0, (
        f"expected exit 0 for {cmd!r}, got {result.returncode}\n"
        f"stdout: {result.stdout!r}"
    )


def test_hook_event_exits_0_for_non_bash_tool():
    """hook-event pretool must not block non-Bash tools."""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}})
    result = _run_hook_event(payload)
    assert result.returncode == 0
