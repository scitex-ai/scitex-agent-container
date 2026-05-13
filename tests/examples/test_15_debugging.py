"""PS-303 stub for examples/15_debugging.sh.

Per-lesson smoke test. The shared check bodies live in
``test_lesson_scripts.py`` so all 15 lessons stay in sync with one
edit point; this stub exists only to satisfy the auditor's
one-test-per-example rule.
"""

from __future__ import annotations

from pathlib import Path

from .test_lesson_scripts import (
    _script_has_no_stale_cli,
    _script_parses,
    _script_runs_readonly,
)

SCRIPT = (
    Path(__file__).resolve().parents[2] / "examples" / "15_debugging.sh"
)


def test_parses() -> None:
    _script_parses(SCRIPT)


def test_runs_readonly(tmp_path: Path) -> None:
    _script_runs_readonly(SCRIPT, tmp_path)


def test_no_stale_cli() -> None:
    _script_has_no_stale_cli(SCRIPT)
