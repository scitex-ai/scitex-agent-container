"""Smoke tests for the installed ``sac`` CLI binary.

Goal: fast (<60s total), runs-on-every-PR sanity checks that verify the
user-facing happy path actually works. These catch breakage that
slips past component-level unit tests — missing imports, broken Click
entrypoints, wrong default kwargs, dependency-resolution failures —
i.e. things that pass unit tests but blow up in the user's terminal.

Each test uses a *real* ``subprocess.run(["sac", ...])`` — not Click's
``CliRunner`` — because the whole point of a smoke layer is "does the
installed binary still launch?". TQ-compliant: AAA markers, one
assert, ≥3-word test names, ``pytest.mark.smoke``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sac() -> str:
    """Resolve the installed ``sac`` entrypoint or skip the suite.

    The smoke layer is *only* meaningful against a real installed CLI;
    if no ``sac`` is on PATH, the layer self-skips rather than masking
    a missing binary as a passing test (no-false-positives).
    """
    found = shutil.which("sac")
    if not found:
        pytest.skip("sac binary not on PATH; smoke layer requires editable install")
    return found


def _run(*args: str, cwd: Path | None = None, timeout: int = 30):
    """Run ``sac <args>`` via real subprocess, capture stdout/stderr/exit."""
    return subprocess.run(
        [_sac(), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# A. CLI banner / discoverability
# ---------------------------------------------------------------------------


def test_sac_help_exits_zero():
    # Arrange
    argv = ("--help",)
    # Act
    result = _run(*argv)
    # Assert
    assert result.returncode == 0, (
        f"sac --help exited {result.returncode}\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def test_sac_help_lists_agents_subcommand():
    # Arrange
    argv = ("--help",)
    # Act
    result = _run(*argv)
    # Assert (substring covers both `agent` and `agents`)
    assert "agent" in result.stdout.lower()


def test_sac_agents_help_exits_zero():
    # Arrange
    argv = ("agents", "--help")
    # Act
    result = _run(*argv)
    # Assert
    assert result.returncode == 0, (
        f"sac agents --help exited {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# B. Dry-run lifecycle (no real containers; config + registry surface only)
# ---------------------------------------------------------------------------


_MINIMAL_V3_SPEC = """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions
"""


def test_sac_agents_start_dry_run_against_real_spec_yaml(tmp_path, env_save_restore):
    # Arrange — write a minimal v3 spec and redirect HOME so the
    # dry-run materialises its workspace under tmp_path, not the
    # developer's real ~/.scitex tree.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    spec = tmp_path / "smoke-agent" / "spec.yaml"
    spec.parent.mkdir()
    spec.write_text(_MINIMAL_V3_SPEC)
    # Act
    result = _run("agents", "start", str(spec), "--dry-run", cwd=tmp_path)
    # Assert (one combined assert: exit 0 AND output mentions "dry-run")
    assert (
        result.returncode == 0 and "dry-run" in (result.stdout + result.stderr).lower()
    ), f"exit={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def test_sac_agents_check_for_unknown_agent_exits_nonzero(tmp_path, env_save_restore):
    # Arrange — empty HOME and empty SCITEX_AGENT_CONTAINER_YAML_DIRS so
    # the agent name truly cannot be resolved.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", "")
    # Act
    result = _run("agents", "check", "nonexistent-agent-xyz-smoke")
    # Assert
    assert result.returncode != 0


# Parametrized: every agent-scoped subcommand must surface "agent not in
# registry" as a non-zero exit. CI gate so a future regression that
# silently swallows the error (exit 0 with stderr-only message) cannot
# slip past — `set -e`-style callers depend on this contract.
_UNKNOWN_AGENT_INVOCATIONS = [
    pytest.param(("status", "nonexistent-agent-xyz-smoke"), id="status"),
    pytest.param(("tail", "nonexistent-agent-xyz-smoke"), id="tail"),
    pytest.param(("health", "nonexistent-agent-xyz-smoke"), id="health"),
    pytest.param(("recall", "nonexistent-agent-xyz-smoke"), id="recall"),
    pytest.param(("send", "nonexistent-agent-xyz-smoke", "hello"), id="send"),
    pytest.param(("stop", "nonexistent-agent-xyz-smoke"), id="stop"),
    pytest.param(
        ("start", "nonexistent-agent-xyz-smoke", "--dry-run"), id="start-dry-run"
    ),
    pytest.param(("check", "nonexistent-agent-xyz-smoke"), id="check"),
]


@pytest.mark.parametrize("argv", _UNKNOWN_AGENT_INVOCATIONS)
def test_sac_agents_subcommand_exits_nonzero_for_unknown_agent(
    argv, tmp_path, env_save_restore
):
    # Arrange — fully isolate the lookup: empty HOME, no extra YAML
    # search dirs, and an empty registry dir.
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", "")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(registry))
    # Act
    result = _run("agents", *argv)
    # Assert
    assert result.returncode != 0, (
        f"sac agents {' '.join(argv)} unexpectedly exited 0\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_sac_agents_list_runs_against_tmp_registry(tmp_path, env_save_restore):
    # Arrange — point both the registry dir AND HOME at tmp_path so the
    # list reads from an isolated, empty registry.
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(registry))
    # Act
    result = _run("agents", "list")
    # Assert
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# C. CLI tree completeness (catches broken subcommand wiring)
# ---------------------------------------------------------------------------


def test_help_recursive_does_not_raise():
    # Arrange — `--help-recursive` walks every subcommand and imports
    # it; any broken import surfaces as a non-zero exit.
    argv = ("--help-recursive",)
    # Act
    result = _run(*argv, timeout=45)
    # Assert
    assert result.returncode == 0, (
        f"sac --help-recursive exited {result.returncode}\n"
        f"stderr tail={result.stderr[-400:]!r}"
    )
