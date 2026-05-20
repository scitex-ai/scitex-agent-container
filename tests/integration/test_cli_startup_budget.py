"""CLI startup-time budget — see _skills/.../21_cli-startup-budget.md.

Tab completion and ``sac --help`` get re-run on every keystroke in a
user's shell, so a slow entry-point import graph manifests as visible
lag. ``cli_pkg/_lazy_group.py`` keeps the import cost low; this test
enforces the budget so a future "just one more eager import" doesn't
silently reverse the win.

Threshold defaults to 500 ms wall-clock for ``scitex-agent-container
--help`` in a clean Python subprocess (Python boot + click + LazyGroup
+ ``--help`` render — the package is already installed, so this never
includes install time). The 500 ms ceiling is a *local-dev* target.

On CI the floor is higher and load-variable (~0.5–0.55 s on a shared
GitHub runner), so the default relaxes to 1.0 s when ``CI`` /
``GITHUB_ACTIONS`` is set — still an order of magnitude below the
~2.5 s eager-import regression this gate exists to catch. Override
explicitly with the ``SAC_STARTUP_BUDGET_S`` env var on either path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

_DEFAULT_BUDGET_S = 0.5
_CI_BUDGET_S = 1.0


def _budget_s() -> float:
    raw = os.environ.get("SAC_STARTUP_BUDGET_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    # CI runners are slower and load-variable than a dev box; relax the
    # ceiling there so runner jitter (a ~10 ms overshoot of the 500 ms
    # local target) doesn't block releases. 1.0 s still catches the real
    # regression class — a forgotten eager import historically pushed
    # `--help` to ~2.5 s.
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return _CI_BUDGET_S
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


# ---------------------------------------------------------------------------
# _budget_s — threshold resolution (env override > CI default > local default)
# ---------------------------------------------------------------------------


_BUDGET_ENV_VARS = ("SAC_STARTUP_BUDGET_S", "CI", "GITHUB_ACTIONS")


@pytest.fixture
def budget_env():
    """Real, isolated ``os.environ`` for budget resolution.

    Saves + strips every budget-affecting var, yields a tiny setter that
    writes the real environment, and restores the original values on
    teardown. No ``monkeypatch`` (STX-NM002): production reads
    ``os.environ`` directly, so the test drives the real collaborator.
    """
    saved = {k: os.environ.get(k) for k in _BUDGET_ENV_VARS}
    for k in _BUDGET_ENV_VARS:
        os.environ.pop(k, None)

    def _set(key: str, value: str) -> None:
        os.environ[key] = value

    try:
        yield _set
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_budget_defaults_to_local_ceiling_off_ci(budget_env):
    # Arrange — no env vars set (neither CI nor override).
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _DEFAULT_BUDGET_S


def test_budget_relaxes_on_ci(budget_env):
    # Arrange
    budget_env("CI", "true")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _CI_BUDGET_S


def test_budget_relaxes_on_github_actions(budget_env):
    # Arrange
    budget_env("GITHUB_ACTIONS", "true")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _CI_BUDGET_S


def test_explicit_env_override_wins_over_ci_default(budget_env):
    # Arrange — override must beat the CI relaxation.
    budget_env("CI", "true")
    budget_env("SAC_STARTUP_BUDGET_S", "0.25")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == 0.25


def test_malformed_env_override_falls_back_not_crashes(budget_env):
    # Arrange — a garbage override off-CI falls back to the local default.
    budget_env("SAC_STARTUP_BUDGET_S", "not-a-float")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _DEFAULT_BUDGET_S
