"""Pytest wrapper around the 5 OP-PRIO-FMT telegram-format hook scripts'
embedded ``--self-test`` modes.

Each script under
``src/scitex_agent_container/_baseline_assets/telegram_hooks/`` carries
an ``--self-test`` mode that exercises its detection logic with a set
of canned (tool_name, text, want_rc) cases and reports ``pass=N
fail=M``. CI runs each via this pytest so a regression to the script
LOGIC (the python embedded in the bash) or the TEST CONTRACT itself
surfaces on PR — without needing the live Claude Code SDK +
telegrammer + matcher chain to actually fire.

Live-fire verify is documented in ``_baseline_assets/telegram_hooks/
README.md`` and requires the operator to deploy the scripts into the
``_shared/to_home/`` baseline + restart agents (the matcher fix is
the OP-PRIO-2 prerequisite that lets the hooks fire at all).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_HOOKS_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
)


def _run_self_test(script_name: str) -> subprocess.CompletedProcess[str]:
    script = _HOOKS_DIR / script_name
    assert script.is_file(), f"hook script missing: {script}"
    assert script.stat().st_mode & 0o111, f"hook script not executable: {script}"
    return subprocess.run(
        ["bash", str(script), "--self-test"],
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_self_test_clean(result: subprocess.CompletedProcess[str]) -> None:
    summary = result.stdout.splitlines()[-1] if result.stdout else ""
    assert result.returncode == 0, (
        f"self-test returned rc={result.returncode}; expected 0.\n"
        f"summary line: {summary!r}\n"
        f"stdout tail:\n{result.stdout[-800:]}\n"
        f"stderr tail:\n{result.stderr[-400:]}"
    )


def test_enforce_telegram_no_bare_issue_self_test_passes():
    # Arrange
    result = _run_self_test("enforce_telegram_no_bare_issue.sh")
    # Act / Assert
    _assert_self_test_clean(result)


def test_enforce_telegram_no_filler_self_test_passes():
    # Arrange
    result = _run_self_test("enforce_telegram_no_filler.sh")
    # Act / Assert
    _assert_self_test_clean(result)


def test_enforce_telegram_numbering_self_test_passes():
    # Arrange
    result = _run_self_test("enforce_telegram_numbering.sh")
    # Act / Assert
    _assert_self_test_clean(result)


def test_enforce_telegram_use_lists_self_test_passes():
    # Arrange
    result = _run_self_test("enforce_telegram_use_lists.sh")
    # Act / Assert
    _assert_self_test_clean(result)


def test_encourage_telegram_terse_style_self_test_passes():
    # Arrange
    result = _run_self_test("encourage_telegram_terse_style.sh")
    # Act / Assert
    _assert_self_test_clean(result)


@pytest.mark.parametrize(
    "script_name",
    [
        "enforce_telegram_no_bare_issue.sh",
        "enforce_telegram_no_filler.sh",
        "enforce_telegram_numbering.sh",
        "enforce_telegram_use_lists.sh",
        "encourage_telegram_terse_style.sh",
    ],
)
def test_hook_script_exists_and_is_executable(script_name: str):
    # Arrange — pin both presence AND the +x perm in a single assertion
    # so PR reviewers see the file-level deployment invariant.
    script = _HOOKS_DIR / script_name
    # Act
    state = (
        script.is_file(),
        bool(script.stat().st_mode & 0o111) if script.exists() else False,
    )
    # Assert
    assert state == (True, True), (
        f"hook script {script} must exist and be executable (is_file, +x)"
    )
