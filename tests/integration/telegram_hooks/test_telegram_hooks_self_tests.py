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

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "telegram_hooks"
)

# Verbatim shape of the ambient stderr a broken subprocess-coverage
# ``.pth`` writes at INTERPRETER STARTUP — i.e. before any hook's own
# code runs. Its exact text is irrelevant; that is the point.
_STARTUP_NOISE = (
    "Error processing line 1 of "
    "/opt/venv-sac/lib/python3.12/site-packages/"
    "_scitex_dev_subprocess_coverage.pth:\n"
    "Traceback (most recent call last):\n"
    '  File "<frozen site>", line 206, in addpackage\n'
    '  File "<string>", line 1, in <module>\n'
    "ModuleNotFoundError: No module named 'coverage'\n"
)


def _run_self_test(
    script_name: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = _HOOKS_DIR / script_name
    assert script.is_file(), f"hook script missing: {script}"
    assert script.stat().st_mode & 0o111, f"hook script not executable: {script}"
    return subprocess.run(
        ["bash", str(script), "--self-test"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _summary_line(result: subprocess.CompletedProcess[str]) -> str:
    """The script's own ``pass=N fail=M`` verdict line."""
    return result.stdout.splitlines()[-1] if result.stdout else ""


def _summary_tail(result: subprocess.CompletedProcess[str]) -> str:
    """Helper for the failure message: surface the script's own
    `pass=N fail=M` summary line plus a tail of stdout/stderr.
    """
    summary = result.stdout.splitlines()[-1] if result.stdout else ""
    return (
        f"summary line: {summary!r}\n"
        f"stdout tail:\n{result.stdout[-800:]}\n"
        f"stderr tail:\n{result.stderr[-400:]}"
    )


def test_enforce_telegram_no_bare_issue_self_test_passes():
    # Arrange
    script_name = "enforce_telegram_no_bare_issue.sh"
    # Act
    result = _run_self_test(script_name)
    # Assert
    assert result.returncode == 0, _summary_tail(result)


def test_enforce_telegram_no_filler_self_test_passes():
    # Arrange
    script_name = "enforce_telegram_no_filler.sh"
    # Act
    result = _run_self_test(script_name)
    # Assert
    assert result.returncode == 0, _summary_tail(result)


def test_enforce_telegram_numbering_self_test_passes():
    # Arrange
    script_name = "enforce_telegram_numbering.sh"
    # Act
    result = _run_self_test(script_name)
    # Assert
    assert result.returncode == 0, _summary_tail(result)


def test_enforce_telegram_use_lists_self_test_passes():
    # Arrange
    script_name = "enforce_telegram_use_lists.sh"
    # Act
    result = _run_self_test(script_name)
    # Assert
    assert result.returncode == 0, _summary_tail(result)


def test_encourage_telegram_terse_style_self_test_passes():
    # Arrange
    script_name = "encourage_telegram_terse_style.sh"
    # Act
    result = _run_self_test(script_name)
    # Assert
    assert result.returncode == 0, _summary_tail(result)


def test_encourage_terse_style_verdict_survives_interpreter_stderr_noise(tmp_path):
    """The terse-style hook is nudge-only (it always exits 0), so stderr
    is the only observable its self-test has — and stderr is SHARED with
    whatever the interpreter itself prints first. Asserting "stderr is
    non-empty" therefore measures ambient noise, not the nudge: a venv
    whose subprocess-coverage ``.pth`` raised ModuleNotFoundError at
    startup turned pass=6 fail=0 into pass=1 fail=5 with no change to
    the hook. The self-test must key off the hook's own sentinel, so the
    verdict is IDENTICAL under a quiet and a noisy interpreter.
    """
    # Arrange — a PATH entry whose python3 writes a startup traceback to
    # stderr before exec'ing the real interpreter.
    shim = tmp_path / "python3"
    shim.write_text(
        "#!/bin/bash\n"
        "cat >&2 <<'NOISE_EOF'\n"
        f"{_STARTUP_NOISE}"
        "NOISE_EOF\n"
        f'exec {sys.executable} "$@"\n'
    )
    shim.chmod(0o755)
    noisy_env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    # Act
    quiet = _run_self_test("encourage_telegram_terse_style.sh")
    noisy = _run_self_test("encourage_telegram_terse_style.sh", env=noisy_env)
    # Assert — same rc AND the same pass=N fail=M line, both of them green.
    verdict = (
        quiet.returncode,
        noisy.returncode,
        _summary_line(quiet) == _summary_line(noisy),
    )
    assert verdict == (0, 0, True), (
        "terse-style self-test verdict moved with ambient stderr noise\n"
        f"quiet: {_summary_tail(quiet)}\n"
        f"noisy: {_summary_tail(noisy)}"
    )


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
