"""CLI startup-time budget — see _skills/.../21_cli-startup-budget.md.

Tab completion and ``sac --help`` get re-run on every keystroke in a
user's shell, so a slow entry-point import graph manifests as visible
lag. ``cli_pkg/_lazy_group.py`` keeps the import cost low; this test
enforces the budget so a future "just one more eager import" doesn't
silently reverse the win.

Threshold is a flat 500 ms wall-clock for ``scitex-agent-container
--help`` in a clean Python subprocess (Python boot + click + LazyGroup
+ ``--help`` render — the package is already installed, so this never
includes install time). The same ceiling holds on CI: the package's
``.pth`` shims no longer import ``coverage`` at every interpreter
startup, so a clean ``--help`` stays well under budget on a shared
runner. Override explicitly with the ``SAC_STARTUP_BUDGET_S`` env var.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

_DEFAULT_BUDGET_S = 0.5

# Apptainer / Singularity reuse ONE SIF across the whole CI matrix on a
# shared self-hosted runner, and every ``apptainer exec`` starts from a COLD
# overlay filesystem — libpython and the entire package tree are faulted in
# from the image on each run, not served from a warm page cache like a bare
# runner. A clean ``sac --help`` therefore runs materially slower under the
# SIF than on a bare host, doing NO extra work: the ``--help`` import module
# set is byte-for-byte identical to older tags (0 new eager imports vs
# v0.21.0), so the SIF cost is environment, not a code regression. Rather
# than silently inflate the bare-host ceiling — which would let a real eager
# import creep through on bare runners — we apply a documented multiplier
# ONLY under a container runtime. ``SAC_STARTUP_BUDGET_S`` still overrides
# both. (The load-independent regression guard is the import graph itself;
# this wall-clock ceiling is a coarse backstop for egregious blow-ups.)
_CONTAINER_BUDGET_MULTIPLIER = 3.0

# apptainer/singularity export exactly one of these into every exec'd process.
_CONTAINER_ENV_VARS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")

# Run inside a *lean* Python child: it spawns `sac --help`, times the
# spawn with perf_counter, and prints a JSON record per run. Timing from
# this minimal parent — instead of from the heavyweight pytest
# interpreter (typeguard, hypothesis, playwright, coverage tracing, … all
# resident) — measures the cold start a real user's shell pays, not the
# fork/scheduler tax of a bloated parent address space. That parent tax
# (~0.3 s here) was the source of the historical CI flake, not any CLI
# regression.
# Take the floor of several runs: scheduler jitter and disk-cache misses
# only ever *add* time, so the minimum is the cleanest estimate of the
# real cold-start cost. More samples => a tighter floor that the CI
# runner's load variance can't push over the line.
_N_SAMPLES = 8
_TIMER_SOURCE = """
import json, subprocess, sys, time
binary = sys.argv[1]
n = int(sys.argv[2])
records = []
for _ in range(n):
    t0 = time.perf_counter()
    proc = subprocess.run([binary, "--help"], capture_output=True, text=True)
    records.append(
        {
            "elapsed": time.perf_counter() - t0,
            "returncode": proc.returncode,
            "stderr": proc.stderr,
        }
    )
json.dump(records, sys.stdout)
"""


def _under_container_runtime() -> bool:
    """True when running inside an apptainer/singularity SIF.

    Reads the real environment (no monkeypatch): apptainer and singularity
    both export ``APPTAINER_CONTAINER`` / ``SINGULARITY_CONTAINER`` (the SIF
    path) into every process they exec, so their presence is a reliable,
    runtime-agnostic signal that this ``--help`` pays the cold-overlay tax.
    """
    return any(os.environ.get(v) for v in _CONTAINER_ENV_VARS)


def _budget_s() -> float:
    raw = os.environ.get("SAC_STARTUP_BUDGET_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if _under_container_runtime():
        return _DEFAULT_BUDGET_S * _CONTAINER_BUDGET_MULTIPLIER
    return _DEFAULT_BUDGET_S


@pytest.fixture
def help_run_samples() -> tuple[list[float], list[dict]]:
    """Time ``scitex-agent-container --help`` cold-start ``_N_SAMPLES`` times.

    The timing loop runs in a lean Python child (``_TIMER_SOURCE``), not
    in this pytest process, so the measured wall time reflects a real
    user's shell — not the fork tax of pytest's heavyweight interpreter.

    The child env is scrubbed of ``COVERAGE_PROCESS_START`` /
    ``COVERAGE_FILE``: under ``pytest --cov`` those are set, which makes
    the package's coverage ``.pth`` shim eagerly import ``coverage`` in
    every grandchild — pure measurement noise that no end user pays. The
    gate exists to catch eager-import regressions in the CLI graph, not
    coverage instrumentation overhead.

    Returns ``(elapsed_seconds, run_records)`` where each record carries
    ``returncode`` + ``stderr`` for the exit-code assertion.
    """
    binary = shutil.which("scitex-agent-container")
    if binary is None:
        pytest.skip("scitex-agent-container CLI not on PATH (install -e .)")

    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("COVERAGE_PROCESS_START", "COVERAGE_FILE")
    }

    proc = subprocess.run(
        [sys.executable, "-c", _TIMER_SOURCE, binary, str(_N_SAMPLES)],
        capture_output=True,
        text=True,
        env=child_env,
    )
    assert proc.returncode == 0, (
        f"timer child exited {proc.returncode}; stderr: {proc.stderr}"
    )
    records = json.loads(proc.stdout)
    samples = [r["elapsed"] for r in records]
    return samples, records


def test_cli_help_exits_zero(
    help_run_samples: tuple[list[float], list[dict]],
) -> None:
    """Every ``--help`` invocation must exit cleanly."""
    # Arrange
    _, results = help_run_samples
    # Act
    failed = [r for r in results if r["returncode"] != 0]
    # Assert
    assert not failed, (
        f"--help exited non-zero on {len(failed)} run(s); "
        f"first stderr: {failed[0]['stderr'] if failed else ''}"
    )


def test_cli_help_under_budget(
    help_run_samples: tuple[list[float], list[dict]],
) -> None:
    """``scitex-agent-container --help`` must complete within the budget.

    Takes the best of several runs to absorb scheduler / disk-cache
    jitter — we care about the floor, not the worst case.
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
# _budget_s — threshold resolution (env override > flat default)
# ---------------------------------------------------------------------------


_BUDGET_ENV_VARS = ("SAC_STARTUP_BUDGET_S",)


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


def test_budget_defaults_to_flat_ceiling(budget_env):
    # Arrange — no override set.
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _DEFAULT_BUDGET_S


def test_explicit_env_override_wins(budget_env):
    # Arrange
    budget_env("SAC_STARTUP_BUDGET_S", "0.25")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == 0.25


def test_malformed_env_override_falls_back_not_crashes(budget_env):
    # Arrange — a garbage override falls back to the flat default.
    budget_env("SAC_STARTUP_BUDGET_S", "not-a-float")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == _DEFAULT_BUDGET_S
