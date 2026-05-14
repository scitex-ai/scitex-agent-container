"""PS-303 stub for examples/20_talking_with_agents.sh.

Per-lesson smoke test (mirrors test_12_a2a_endpoint.py and the rest
of the lesson stubs). The shared check bodies live in
``test_lesson_scripts.py``; this stub exists to satisfy the
auditor's one-test-per-example rule.

Note: this stub only verifies the script parses and runs read-only
(no --apply). The live end-to-end checks the script performs under
--apply need a running `sac a2a serve` on :8888 plus credentials; they
are not exercised here. That's the same test-quality gap the rest of
the lesson stubs have — flagged for follow-up.
"""

from __future__ import annotations

from pathlib import Path

from .test_lesson_scripts import (
    _script_has_no_stale_cli,
    _script_parses,
    _script_runs_readonly,
)

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "20_talking_with_agents.sh"


def test_parses() -> None:
    _script_parses(SCRIPT)


def test_runs_readonly(tmp_path: Path) -> None:
    _script_runs_readonly(SCRIPT, tmp_path)


def test_no_stale_cli() -> None:
    _script_has_no_stale_cli(SCRIPT)
