"""CLI startup-time budget — see _skills/.../21_cli-startup-budget.md.

Tab completion and ``sac --help`` get re-run on every keystroke in a
user's shell, so a slow entry-point import graph manifests as visible
lag. ``cli_pkg/_lazy_group.py`` keeps the import cost low; this module
enforces the budget so a future "just one more eager import" doesn't
silently reverse the win.

Two-tier guard, because an absolute wall-clock is unenforceable on
shared CI runners (measured 2026-10-17: ``--help`` at 1.0-5.5 s while a
bare ``python -c pass`` booted in 0.07 s on the same box — cold page
cache + 96 contending workers inflate import I/O without moving the
boot floor, so no boot multiple can absorb it):

* ``test_cli_help_import_closure_has_no_new_modules`` (DETERMINISTIC)
  records the exact ``scitex_agent_container.*`` import closure of
  ``--help`` and fails on any ADDITION, naming the new modules.
  Removals pass freely — shrinking the closure is the goal. This is the
  teeth: an eager import cannot hide behind a slow box.
* ``test_cli_help_under_budget`` (wall-clock BACKSTOP) keeps a coarse
  ceiling for egregious blow-ups only.

Under an apptainer/singularity SIF the backstop is multiplied (see
``_CONTAINER_BUDGET_MULTIPLIER``): a reused CI SIF faults libpython +
the package tree from a COLD overlay on every ``apptainer exec``, so
the identical ``--help`` runs materially slower there than on a warm
bare runner. Override explicitly with the ``SAC_STARTUP_BUDGET_S`` env
var (exact when set; wins over the multiplier too).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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

# Bare-interpreter boot multiple: the wall-clock backstop also covers a
# multiple of a ``python -c pass`` boot measured in the same child, so a
# scheduler-starved (but cache-warm) runner does not fail a healthy CLI.
# Boot normalisation does NOT cover cold-cache import I/O — that failure
# mode is what the import-closure test above exists for.
_BOOT_MULTIPLE = 4.0

# Coarse wall-clock backstop (seconds). The 0.5 s UX aspiration is not
# enforceable as an absolute on shared runners; this ceiling catches
# egregious blow-ups only. The regression teeth are the import-closure
# test, which is load-independent.
_WALL_BACKSTOP_S = 2.0

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
boots = []
for _ in range(n):
    t0 = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    boots.append(time.perf_counter() - t0)
json.dump({"records": records, "boots": boots}, sys.stdout)
"""


def _lean_child_env() -> dict[str, str]:
    """Env for timing/probe children: scrub coverage instrumentation.

    Under ``pytest --cov`` the coverage ``.pth`` shim would otherwise
    eagerly import ``coverage`` in every grandchild — pure measurement
    noise (and extra modules in the import closure) that no end user
    pays.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("COVERAGE_PROCESS_START", "COVERAGE_FILE")
    }


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
def help_run_samples() -> tuple[list[float], list[dict], float]:
    """Time ``scitex-agent-container --help`` cold-start ``_N_SAMPLES`` times.

    Also times a bare ``python -c pass`` boot in the same child: on a
    scheduler-starved CI runner BOTH inflate together, so the budget test
    normalises against the boot floor instead of asserting an absolute
    wall-clock the box cannot meet. Returns
    ``(help_seconds, run_records, boot_floor_seconds)``.

    The timing loop runs in a lean Python child (``_TIMER_SOURCE``), not
    in this pytest process, so the measured wall time reflects a real
    user's shell — not the fork tax of pytest's heavyweight interpreter.

    The child env is scrubbed of ``COVERAGE_PROCESS_START`` /
    ``COVERAGE_FILE``: under ``pytest --cov`` those are set, which makes
    the package's coverage ``.pth`` shim eagerly import ``coverage`` in
    every grandchild — pure measurement noise that no end user pays. The
    gate exists to catch eager-import regressions in the CLI graph, not
    coverage instrumentation overhead.
    """
    binary = shutil.which("scitex-agent-container")
    if binary is None:
        pytest.skip("scitex-agent-container CLI not on PATH (install -e .)")

    proc = subprocess.run(
        [sys.executable, "-c", _TIMER_SOURCE, binary, str(_N_SAMPLES)],
        capture_output=True,
        text=True,
        env=_lean_child_env(),
    )
    assert proc.returncode == 0, (
        f"timer child exited {proc.returncode}; stderr: {proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    records = payload["records"]
    samples = [r["elapsed"] for r in records]
    boot_floor = min(payload["boots"])
    return samples, records, boot_floor


_HELP_IMPORT_CLOSURE = frozenset((
    "_scitex_agent_container_bootstrap",
    "scitex_agent_container",
    "scitex_agent_container._drift",
    "scitex_agent_container._drift._authority",
    "scitex_agent_container._drift._fleet",
    "scitex_agent_container._drift._local",
    "scitex_agent_container._drift._sibling",
    "scitex_agent_container._drift._status",
    "scitex_agent_container._drift.versions",
    "scitex_agent_container._env",
    "scitex_agent_container._freshness",
    "scitex_agent_container._freshness._config",
    "scitex_agent_container._freshness._expectations",
    "scitex_agent_container._logging",
    "scitex_agent_container._logging._streams",
    "scitex_agent_container._mcp",
    "scitex_agent_container._mcp._tools",
    "scitex_agent_container._mcp._tools._account",
    "scitex_agent_container._mcp._tools._agent",
    "scitex_agent_container._mcp._tools._agent_fork",
    "scitex_agent_container._mcp._tools._agent_twin",
    "scitex_agent_container._mcp._tools._helpers",
    "scitex_agent_container._mcp._tools._host",
    "scitex_agent_container._mcp._tools._image",
    "scitex_agent_container._mcp._tools._info",
    "scitex_agent_container._mcp._tools._skills",
    "scitex_agent_container._mcp._tools._store",
    "scitex_agent_container._mcp._tools._subagent",
    "scitex_agent_container._mcp._tools._template",
    "scitex_agent_container._mcp.server",
    "scitex_agent_container._never_stop_when_task_remains",
    "scitex_agent_container._never_stop_when_task_remains._awaiting_operator",
    "scitex_agent_container._never_stop_when_task_remains._decide",
    "scitex_agent_container._never_stop_when_task_remains._detector",
    "scitex_agent_container._never_stop_when_task_remains._identity",
    "scitex_agent_container._never_stop_when_task_remains._loop_guard",
    "scitex_agent_container._runtime_paths",
    "scitex_agent_container._state",
    "scitex_agent_container._state._host_config_blocks",
    "scitex_agent_container._state._host_interfaces",
    "scitex_agent_container._state._host_ssh",
    "scitex_agent_container._state._meta",
    "scitex_agent_container._state._meta.secrets",
    "scitex_agent_container._state.host_config",
    "scitex_agent_container._state.host_registry",
    "scitex_agent_container._state.moving_alias",
    "scitex_agent_container._state.ssh_control_options",
    "scitex_agent_container.cli",
    "scitex_agent_container.cli_pkg",
    "scitex_agent_container.cli_pkg._helpers",
    "scitex_agent_container.cli_pkg._helpers._groups",
    "scitex_agent_container.cli_pkg._helpers._json_flag",
    "scitex_agent_container.cli_pkg._lazy_group",
    "scitex_agent_container.cli_pkg._main",
    "scitex_agent_container.cli_pkg.freshness_cmds",
    "scitex_agent_container.cli_pkg.never_stop_when_task_remains_cmds",
    "scitex_agent_container.cli_pkg.subagent_group",
    "scitex_agent_container.cli_pkg.versions_cmds",
    "scitex_agent_container.config",
    "scitex_agent_container.config._acl_types",
    "scitex_agent_container.config._acl_validation",
    "scitex_agent_container.config._apptainer_spec",
    "scitex_agent_container.config._claude_flags_validation",
    "scitex_agent_container.config._claude_spec",
    "scitex_agent_container.config._claude_validation",
    "scitex_agent_container.config._container_engine",
    "scitex_agent_container.config._delegation_types",
    "scitex_agent_container.config._engine_entry_validation",
    "scitex_agent_container.config._engine_library",
    "scitex_agent_container.config._engine_types",
    "scitex_agent_container.config._engine_validation",
    "scitex_agent_container.config._explicit_fields",
    "scitex_agent_container.config._explicit_validation",
    "scitex_agent_container.config._external_gateway",
    "scitex_agent_container.config._harness_callables",
    "scitex_agent_container.config._harness_lookup",
    "scitex_agent_container.config._harness_registry",
    "scitex_agent_container.config._harness_types",
    "scitex_agent_container.config._hermes_background_review",
    "scitex_agent_container.config._hermes_compression",
    "scitex_agent_container.config._hermes_context_budget",
    "scitex_agent_container.config._hermes_failover",
    "scitex_agent_container.config._hermes_goals",
    "scitex_agent_container.config._hermes_run_budget",
    "scitex_agent_container.config._hermes_yolo",
    "scitex_agent_container.config._host",
    "scitex_agent_container.config._labels_validation",
    "scitex_agent_container.config._loader_startup_defaults",
    "scitex_agent_container.config._loaders",
    "scitex_agent_container.config._opencode_approval",
    "scitex_agent_container.config._opencode_run_budget",
    "scitex_agent_container.config._opencode_serve",
    "scitex_agent_container.config._parsers",
    "scitex_agent_container.config._parsers._a2a",
    "scitex_agent_container.config._parsers._apptainer",
    "scitex_agent_container.config._parsers._autonomous",
    "scitex_agent_container.config._parsers._claude",
    "scitex_agent_container.config._parsers._comms",
    "scitex_agent_container.config._parsers._container",
    "scitex_agent_container.config._parsers._delegation",
    "scitex_agent_container.config._parsers._extensions",
    "scitex_agent_container.config._parsers._health",
    "scitex_agent_container.config._parsers._helpers",
    "scitex_agent_container.config._parsers._hooks",
    "scitex_agent_container.config._parsers._hosts",
    "scitex_agent_container.config._parsers._listen",
    "scitex_agent_container.config._parsers._mcp",
    "scitex_agent_container.config._parsers._proxy",
    "scitex_agent_container.config._parsers._restart",
    "scitex_agent_container.config._parsers._skills",
    "scitex_agent_container.config._parsers._skills_defaults",
    "scitex_agent_container.config._parsers._startup",
    "scitex_agent_container.config._parsers._watchdog",
    "scitex_agent_container.config._placement_validation",
    "scitex_agent_container.config._provider_parse",
    "scitex_agent_container.config._provider_registry",
    "scitex_agent_container.config._provider_types",
    "scitex_agent_container.config._provider_validation",
    "scitex_agent_container.config._proxy_types",
    "scitex_agent_container.config._python_venv",
    "scitex_agent_container.config._qwen_gateway",
    "scitex_agent_container.config._reserved_names",
    "scitex_agent_container.config._residency_types",
    "scitex_agent_container.config._resolve",
    "scitex_agent_container.config._shape_validation",
    "scitex_agent_container.config._spec_keys",
    "scitex_agent_container.config._startup_command_validation",
    "scitex_agent_container.config._types",
    "scitex_agent_container.config._validation",
    "scitex_agent_container.config._workdir_hook",
    "scitex_agent_container.runtimes",
    "scitex_agent_container.runtimes._board_identity_env",
    "scitex_agent_container.runtimes._fleet_env",
    "scitex_agent_container.runtimes._pg_identity_env",
    "scitex_agent_container.runtimes.base",
))
_CLOSURE_PROBE = """
import json, sys
sys.argv = ["sac", "--help"]
try:
    import _scitex_agent_container_bootstrap as entry
    entry.cli_entry_point()
except SystemExit:
    pass
sys.stderr.write(json.dumps(sorted(sys.modules)))
"""


def _help_import_closure() -> set[str]:
    """Run the REAL ``sac --help`` entry and return its sac import closure.

    ``--help`` text goes to stdout; the module list to stderr. A
    non-zero exit (a broken entry) fails loudly here rather than
    producing an empty closure that would pass the comparison below.

    ``cwd`` is the checkout's ``src/`` (NOT the installed package):
    pytest's ``pythonpath`` pinning lives in-process and does not cross
    into grandchildren, so without this the probe would grade whatever
    copy the interpreter happens to have installed.
    """
    repo_src = Path(__file__).resolve().parents[2] / "src"
    proc = subprocess.run(
        [sys.executable, "-c", _CLOSURE_PROBE],
        capture_output=True,
        text=True,
        env=_lean_child_env(),
        cwd=repo_src,
    )
    assert proc.returncode == 0, (
        f"closure probe exited {proc.returncode}; stderr: {proc.stderr[-2000:]}"
    )
    return {
        m
        for m in json.loads(proc.stderr)
        if m == "scitex_agent_container"
        or m.startswith(
            ("scitex_agent_container.", "_scitex_agent_container_bootstrap")
        )
    }


def test_cli_help_import_closure_has_no_new_modules() -> None:
    """``--help`` must not import sac modules beyond the recorded closure.

    LOAD-INDEPENDENT startup-budget teeth: an eager import adds modules
    to this set no matter how fast or slow the box is, so a regression
    cannot hide behind CI load the way wall-clock can. ADDITIONS fail,
    naming the new modules (extend ``_HELP_IMPORT_CLOSURE`` deliberately
    when the growth is intended). REMOVALS pass freely — shrinking the
    closure is the goal.
    """
    # Arrange
    observed = _help_import_closure()
    # Act
    new_modules = observed - _HELP_IMPORT_CLOSURE
    # Assert
    assert not new_modules, (
        "sac --help imports sac modules outside the recorded closure — "
        "a new eager import in cli_pkg/_main.py or its transitive imports? "
        f"New: {sorted(new_modules)}. If the growth is intended, extend "
        "_HELP_IMPORT_CLOSURE deliberately (it is the budget ledger)."
    )


def test_cli_help_exits_zero(
    help_run_samples: tuple[list[float], list[dict], float],
) -> None:
    """Every ``--help`` invocation must exit cleanly."""
    # Arrange
    _, results, _boot = help_run_samples
    # Act
    failed = [r for r in results if r["returncode"] != 0]
    # Assert
    assert not failed, (
        f"--help exited non-zero on {len(failed)} run(s); "
        f"first stderr: {failed[0]['stderr'] if failed else ''}"
    )


def test_cli_help_under_budget(
    help_run_samples: tuple[list[float], list[dict], float],
) -> None:
    """``scitex-agent-container --help`` must complete within the backstop.

    Takes the best of several runs to absorb scheduler / disk-cache
    jitter — we care about the floor, not the worst case. This is the
    COARSE backstop for egregious blow-ups only (see module docstring):
    an explicit ``SAC_STARTUP_BUDGET_S`` is honoured exactly, otherwise
    the ceiling is the largest of the resolved budget, the boot multiple,
    and the flat backstop. Eager-import regressions are caught
    deterministically by ``test_cli_help_import_closure_has_no_new_modules``.
    """
    # Arrange
    samples, _, boot_floor = help_run_samples
    budget = _budget_s()
    if os.environ.get("SAC_STARTUP_BUDGET_S"):
        ceiling = budget
    else:
        ceiling = max(budget, boot_floor * _BOOT_MULTIPLE, _WALL_BACKSTOP_S)
    # Act
    best = min(samples)
    # Assert
    assert best < ceiling, (
        f"`scitex-agent-container --help` took {best:.3f}s "
        f"(budget {budget:.3f}s, boot floor {boot_floor:.3f}s, "
        f"ceiling {ceiling:.3f}s). Samples: {[f'{s:.3f}' for s in samples]}. "
        "If the import closure is unchanged (see the closure test), this "
        "is box slowness, not a regression — re-run. If the closure grew, "
        "a new eager import in cli_pkg/_main.py or one of its transitive "
        "imports is the cause. See _skills/.../21_cli-startup-budget.md."
    )


# ---------------------------------------------------------------------------
# _budget_s — threshold resolution (env override > flat default)
# ---------------------------------------------------------------------------


# Strip the container-runtime markers too so these resolution tests give the
# same answer whether the suite itself runs on a bare host OR inside the SIF.
_BUDGET_ENV_VARS = ("SAC_STARTUP_BUDGET_S", *_CONTAINER_ENV_VARS)


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


def test_container_runtime_multiplies_the_flat_ceiling(budget_env):
    # Arrange — simulate an apptainer SIF exec (cold overlay filesystem).
    budget_env("APPTAINER_CONTAINER", "/home/ci/.scitex/dev/containers/ci-cpu.sif")
    # Act
    budget = _budget_s()
    # Assert — the cold-start penalty is absorbed by a documented multiplier,
    # NOT by silently raising the bare-host ceiling.
    assert budget == _DEFAULT_BUDGET_S * _CONTAINER_BUDGET_MULTIPLIER


def test_explicit_override_wins_even_under_container(budget_env):
    # Arrange — an explicit budget must win over the container multiplier too.
    budget_env("SINGULARITY_CONTAINER", "/some/image.sif")
    budget_env("SAC_STARTUP_BUDGET_S", "0.9")
    # Act
    budget = _budget_s()
    # Assert
    assert budget == 0.9
