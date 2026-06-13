"""Pure parsers/formatters for the Spartan pytest summary.

Kept dependency-free (stdlib only) so unit tests can exercise every
behaviour locally without ssh/SLURM.  See the package docstring in
``__init__.py`` for the full architecture.
"""

from __future__ import annotations

import json
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

    @property
    def all_passed(self) -> bool:
        """True iff zero ``failed`` and zero ``errors``."""
        return self.failed == 0 and self.errors == 0


def _parse_summary(blob: str) -> PytestSummary:
    """Parse the remote ``summary.json`` string into a :class:`PytestSummary`.

    Defensive: missing fields default to 0/empty.  Returns an
    ``errors=1`` summary on malformed JSON so the caller's exit-code
    mapper still produces a sensible (failure) verdict — pure-function
    contract preserved.
    """
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return PytestSummary(errors=1)
    if not isinstance(data, dict):
        return PytestSummary(errors=1)
    return PytestSummary(
        passed=int(data.get("passed", 0) or 0),
        failed=int(data.get("failed", 0) or 0),
        errors=int(data.get("errors", 0) or 0),
        duration_s=float(data.get("duration_s", 0.0) or 0.0),
        failed_tests=list(data.get("failed_tests", []) or []),
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
