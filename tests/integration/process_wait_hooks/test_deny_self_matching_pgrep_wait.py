"""Regression coverage for self-matching background process waiters."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = (
    REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "process_wait_hooks"
    / "deny_self_matching_pgrep_wait.sh"
)


def _run_hook(command: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_pgrep_full_regex_really_matches_its_waiting_shell() -> None:
    # Arrange
    # A unique literal exists only in this shell's `bash -c` argv.
    marker = f"sac-pgrep-self-match-{uuid.uuid4().hex}"
    # The trailing no-op keeps Bash from replacing itself with the final pgrep.
    command = f'pgrep -f "{marker}"; :'
    # Act
    process = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=5)
    matched_pids = {int(line) for line in stdout.splitlines()}
    observed = (process.returncode, process.pid in matched_pids)
    # Assert
    # pgrep excludes itself, but not its parent waiting shell.
    assert observed == (0, True), stderr


def test_refuses_observed_until_waiter_before_it_can_hang() -> None:
    # Arrange
    command = 'until ! pgrep -f "cards-publish-worker"; do sleep 5; done'
    # Act
    result = _run_hook(command)
    observed = (
        result.returncode,
        "matches the waiter's own command line" in result.stderr,
        'wait "$pid"' in result.stderr,
    )
    # Assert
    assert observed == (2, True, True)


def test_refuses_while_waiter_with_combined_pgrep_flags() -> None:
    # Arrange
    command = "while pgrep -af cards-publish-worker; do sleep 1; done"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 2


def test_refuses_long_full_option_spelling() -> None:
    # Arrange
    command = "until ! pgrep --full cards-publish-worker; do sleep 1; done"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 2


def test_allows_self_excluding_bracket_pattern() -> None:
    # Arrange
    command = "while pgrep -f '[c]ards-publish-worker'; do sleep 1; done"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_allows_one_shot_pgrep_full_probe() -> None:
    # Arrange
    command = "pgrep -af cards-publish-worker"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_allows_pgrep_exact_name_loop() -> None:
    # Arrange
    command = "while pgrep -x cards-worker; do sleep 1; done"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_allows_full_pattern_anchored_away_from_shell_command() -> None:
    # Arrange
    command = "until ! pgrep --full '^/usr/bin/cards-worker$'; do sleep 1; done"
    # Act
    result = _run_hook(command)
    # Assert
    assert result.returncode == 0


def test_non_bash_tool_is_untouched() -> None:
    # Arrange
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"command": "pgrep -f worker"}}
    )
    # Act
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    # Assert
    assert result.returncode == 0


def test_hook_asset_is_executable() -> None:
    # Arrange
    path = HOOK
    # Act
    executable = os.access(path, os.X_OK)
    # Assert
    assert executable
