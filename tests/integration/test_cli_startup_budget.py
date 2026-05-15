"""CLI startup-time budget — see _skills/.../21_cli-startup-budget.md.

Tab completion and ``sac --help`` get re-run on every keystroke in a
user's shell, so a slow entry-point import graph manifests as visible
lag. ``cli_pkg/_lazy_group.py`` keeps the import cost low; this test
enforces the budget so a future "just one more eager import" doesn't
silently reverse the win.

Threshold defaults to 500 ms wall-clock for ``scitex-agent-container
--help`` in a clean Python subprocess. Override via the
``SAC_STARTUP_BUDGET_S`` env var — useful on slow CI runners or under
heavy load.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

_DEFAULT_BUDGET_S = 0.5


def _budget_s() -> float:
    raw = os.environ.get("SAC_STARTUP_BUDGET_S")
    if not raw:
        return _DEFAULT_BUDGET_S
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_S


@pytest.fixture
def help_run_samples() -> tuple[list[float], list[subprocess.CompletedProcess[str]]]:
    """Run ``scitex-agent-container --help`` three times and collect timings + results.

    Spawns fresh subprocesses so the parent's already-warmed
    ``sys.modules`` doesn't mask import cost.
    """
    binary = shutil.which("scitex-agent-container")
    if binary is None:
        pytest.skip("scitex-agent-container CLI not on PATH (install -e .)")

    samples: list[float] = []
    results: list[subprocess.CompletedProcess[str]] = []
    for _ in range(3):
        t0 = time.perf_counter()
        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
        )
        samples.append(time.perf_counter() - t0)
        results.append(result)
    return samples, results


def test_cli_help_exits_zero(
    help_run_samples: tuple[list[float], list[subprocess.CompletedProcess[str]]],
) -> None:
    """Every ``--help`` invocation must exit cleanly."""
    # Arrange
    _, results = help_run_samples
    # Act
    failed = [r for r in results if r.returncode != 0]
    # Assert
    assert not failed, (
        f"--help exited non-zero on {len(failed)} run(s); "
        f"first stderr: {failed[0].stderr if failed else ''}"
    )


def test_cli_help_under_budget(
    help_run_samples: tuple[list[float], list[subprocess.CompletedProcess[str]]],
) -> None:
    """``scitex-agent-container --help`` must complete within the budget.

    Takes the best of three runs to absorb scheduler / disk-cache jitter —
    we care about the floor, not the worst case.
    """
    # Arrange
    samples, _ = help_run_samples
    budget = _budget_s()
    # Act
    best = min(samples)
    # Assert
    assert best < budget, (
        f"`scitex-agent-container --help` took {best:.3f}s "
        f"(budget {budget:.3f}s). Samples: {[f'{s:.3f}' for s in samples]}. "
        "Likely cause: a new eager import in cli_pkg/_main.py or one of "
        "its transitive imports. See _skills/.../21_cli-startup-budget.md."
    )
