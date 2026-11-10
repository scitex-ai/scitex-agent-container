"""Pure parsers/formatters for the Spartan pytest summary.

Summary data and parsing use the standard library; target validation raises
Click usage errors. All helpers can be exercised locally without SSH/SLURM.
See the package docstring for the complete runner architecture.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import click


@dataclass(frozen=True)
class PytestSummary:
    """Summary parsed from the remote ``summary.json``.

    Mirrors the shape the sbatch script writes at the end of the job.
    Used by the formatter + exit-code mapper.
    """

    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_s: float = 0.0
    failed_tests: list[str] = field(default_factory=list)
    pytest_exit: int | None = None
    capture_exit: int | None = None

    @property
    def all_passed(self) -> bool:
        """No count or explicit process/capture failures.

        Legacy count-only Python records remain readable. CLI acceptance
        additionally requires process evidence and successful accounting.
        """
        return (
            self.failed == 0
            and self.errors == 0
            and self.pytest_exit in (None, 0)
            and self.capture_exit in (None, 0)
        )


def _parse_summary(blob: str) -> PytestSummary:
    """Parse the remote ``summary.json`` string into a :class:`PytestSummary`.

    Complete, typed counters are required. Invalid records return an error
    result rather than silently defaulting to a green run. Legacy records
    with explicit counters remain readable without process-exit metadata.
    """
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return PytestSummary(errors=1)
    if not isinstance(data, dict):
        return PytestSummary(errors=1)
    counters = [data.get(key) for key in ("passed", "failed", "errors")]
    if any(type(value) is not int or value < 0 for value in counters):
        return PytestSummary(errors=1)
    duration = data.get("duration_s", 0.0)
    if type(duration) not in (int, float):
        return PytestSummary(errors=1)
    try:
        duration = float(duration)
    except OverflowError:
        return PytestSummary(errors=1)
    if not math.isfinite(duration) or duration < 0:
        return PytestSummary(errors=1)
    failed_tests = data.get("failed_tests", [])
    if not isinstance(failed_tests, list) or any(
        not isinstance(name, str) for name in failed_tests
    ):
        return PytestSummary(errors=1)
    exits = {}
    for key in ("pytest_exit", "capture_exit"):
        value = data.get(key)
        if key in data and (type(value) is not int or not 0 <= value <= 255):
            return PytestSummary(errors=1)
        exits[key] = value
    return PytestSummary(
        passed=counters[0],
        failed=counters[1],
        errors=counters[2],
        duration_s=duration,
        failed_tests=failed_tests,
        **exits,
    )


def _format_summary(summary: PytestSummary, *, repo: str, branch: str) -> str:
    """Render a human-readable summary block.  Pure string."""
    verdict = "PASS" if summary.all_passed else "FAIL"
    lines = [
        f"Spartan pytest {verdict}: {repo}@{branch}",
        f"  passed:     {summary.passed}",
        f"  failed:     {summary.failed}",
        f"  errors:     {summary.errors}",
        f"  duration_s: {summary.duration_s:.1f}",
        f"  pytest_exit: {summary.pytest_exit if summary.pytest_exit is not None else 'not recorded'}",
        f"  capture_exit: {summary.capture_exit if summary.capture_exit is not None else 'not recorded'}",
    ]
    if summary.failed_tests:
        lines.append("  failed tests:")
        for name in summary.failed_tests[:20]:
            lines.append(f"    - {name}")
        if len(summary.failed_tests) > 20:
            lines.append(f"    ... ({len(summary.failed_tests) - 20} more)")
    return "\n".join(lines)


def _resolve_exit_code(summary: PytestSummary) -> int:
    """Map a parsed summary onto a shell exit code: 0 = green, 1 = anything else."""
    return 0 if summary.all_passed else 1


def _split_repo_at_branch(arg: str) -> tuple[str, str]:
    """Split ``REPO@BRANCH``; raises ``click.UsageError`` on malformed input."""
    if "@" not in arg:
        raise click.UsageError("expected REPO@BRANCH (e.g. ywatanabe1989/sac@develop)")
    repo, _, branch = arg.rpartition("@")
    if not repo or not branch:
        raise click.UsageError("expected REPO@BRANCH (e.g. ywatanabe1989/sac@develop)")
    return repo, branch


__all__ = [
    "PytestSummary",
    "_format_summary",
    "_parse_summary",
    "_resolve_exit_code",
    "_split_repo_at_branch",
]
