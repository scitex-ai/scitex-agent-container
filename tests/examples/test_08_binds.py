"""PS-303 stub for examples/08_binds.sh.

Per-lesson smoke test covering three guarantees: the script parses under
``bash -n``, executes cleanly without ``--apply`` under a throwaway
``HOME``, and contains no references to retired CLI surface.

The shared probe bodies live in ``test_lesson_scripts.py`` so all 15
lessons stay in sync with one edit point; this stub exists only to
satisfy the auditor's one-test-per-example rule (PS-303).

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
"""

from __future__ import annotations

from pathlib import Path

from .test_lesson_scripts import (
    _probe_parses,
    _probe_runs_readonly,
    _probe_stale_cli_offenders,
)

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "08_binds.sh"


def test_lesson_script_parses_under_bash_n() -> None:
    # Arrange
    script = SCRIPT
    # Act
    result = _probe_parses(script)
    # Assert
    assert result.returncode == 0, f"bash -n failed for {script.name}:\n{result.stderr}"


def test_lesson_script_runs_readonly_under_tmp_home(tmp_path: Path) -> None:
    # Arrange
    script = SCRIPT
    # Act
    result = _probe_runs_readonly(script, tmp_path)
    # Assert
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_lesson_script_has_no_stale_cli_strings() -> None:
    # Arrange
    script = SCRIPT
    # Act
    offenders = _probe_stale_cli_offenders(script)
    # Assert
    assert not offenders, f"{script.name} contains stale CLI strings: {offenders}"
