"""Tests for ``sac render-sbatch`` and ``sac render-attach`` CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.fixture
def slurm_state_env():
    """Set ``SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR`` and restore on teardown.

    PA-306: replaces ``monkeypatch.setenv``. Returns a setter callable
    so tests can pass the state dir they just built.
    """
    # Arrange
    saved = os.environ.get("SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR")

    def _set(state_dir: Path) -> None:
        os.environ["SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR"] = str(state_dir)

    # Act
    yield _set

    # Assert (teardown: restore prior env state)
    if saved is None:
        os.environ.pop("SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR", None)
    else:
        os.environ["SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR"] = saved


_SLURM_YAML = dedent(
    """\
    apiVersion: scitex-agent-container/v3
    kind: Agent
    metadata:
      labels:
        role: head
    spec:
      runtime: slurm
      model: opus[1m]
      slurm:
        partition: sapphire
        time_limit: 7-00:00:00
        cpus_per_task: 2
        mem: 4G
        hooks:
          pre_agent: /path/to/pre-agent.sh
          walltime_signal: /path/to/walltime-notify.sh
    """
)

_CLAUDE_YAML = dedent(
    """\
    apiVersion: scitex-agent-container/v3
    kind: Agent
    metadata:
      labels:
        role: head
    spec:
      runtime: apptainer
      model: sonnet
    """
)


@pytest.fixture
def slurm_yaml(tmp_path: Path) -> Path:
    """v3: dir-as-SSoT — agent name from parent dir."""
    d = tmp_path / "head-spartan"
    d.mkdir()
    p = d / "head-spartan.yaml"
    p.write_text(_SLURM_YAML)
    return p


@pytest.fixture
def claude_yaml(tmp_path: Path) -> Path:
    d = tmp_path / "head-local"
    d.mkdir()
    p = d / "head-local.yaml"
    p.write_text(_CLAUDE_YAML)
    return p


# ---------------------------------------------------------------------------
# render-sbatch
# ---------------------------------------------------------------------------


_SKIP_RENDER_SBATCH = pytest.mark.skip(
    reason=(
        "F-CS17: SLURM rendering is slated for deletion. The validator "
        "now hard-errors on runtime: slurm; the render_sbatch / "
        "render_attach helpers go in F-CS17 stage 3 alongside this test "
        "class."
    )
)


@pytest.fixture
def render_sbatch_slurm_result(slurm_yaml: Path):
    """Run ``render-sbatch`` once against the SLURM YAML fixture."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["template", "render-sbatch", str(slurm_yaml)])
    # Assert (return for assertion fan-out across parametrized tests)
    return result


@pytest.fixture
def render_sbatch_non_slurm_result(claude_yaml: Path):
    """Run ``render-sbatch`` once against the non-SLURM YAML fixture."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["template", "render-sbatch", str(claude_yaml)])
    # Assert (return for assertion fan-out across parametrized tests)
    return result


@_SKIP_RENDER_SBATCH
class TestRenderSbatch:
    def test_exit_code_is_zero_for_slurm_runtime(
        self, render_sbatch_slurm_result
    ) -> None:
        # Arrange
        result = render_sbatch_slurm_result
        # Act
        exit_code = result.exit_code
        # Assert
        assert exit_code == 0, result.output

    def test_output_starts_with_bash_shebang(self, render_sbatch_slurm_result) -> None:
        # Arrange
        output = render_sbatch_slurm_result.output
        # Act
        starts_with_shebang = output.startswith("#!/bin/bash\n")
        # Assert
        assert starts_with_shebang

    @pytest.mark.parametrize(
        "expected_fragment",
        [
            "#SBATCH --partition=sapphire",
            "set -euo pipefail",
            "tail -f /dev/null",
            "trap _sac_slurm_walltime_handler USR1",
            # Hooks declared in YAML appear in the emitted script.
            "/path/to/pre-agent.sh",
            "/path/to/walltime-notify.sh",
        ],
    )
    def test_output_contains_expected_fragment(
        self, render_sbatch_slurm_result, expected_fragment: str
    ) -> None:
        # Arrange
        output = render_sbatch_slurm_result.output
        # Act
        present = expected_fragment in output
        # Assert
        assert present, output

    def test_non_slurm_runtime_exits_non_zero(
        self, render_sbatch_non_slurm_result
    ) -> None:
        # Arrange
        result = render_sbatch_non_slurm_result
        # Act
        exit_code = result.exit_code
        # Assert
        assert exit_code != 0

    def test_non_slurm_runtime_emits_requires_slurm_message(
        self, render_sbatch_non_slurm_result
    ) -> None:
        # Arrange
        output = render_sbatch_non_slurm_result.output
        # Act
        has_message = "requires runtime: slurm" in output
        # Assert
        assert has_message, output


# ---------------------------------------------------------------------------
# render-attach
# ---------------------------------------------------------------------------


_SKIP_RENDER_ATTACH = pytest.mark.skip(
    reason=(
        "F-CS17: SLURM rendering is slated for deletion. render_attach / "
        "render_sbatch helpers + the slurm runtime go in F-CS17 stage 3."
    )
)


def _write_state(tmp_path: Path, slurm_state_env) -> None:
    """Write a recorded job-id state file and point the env var at it."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "head-spartan.json").write_text(
        json.dumps({"name": "head-spartan", "job_id": "54321"})
    )
    slurm_state_env(state_dir)


@pytest.fixture
def render_attach_recorded_result(slurm_yaml: Path, tmp_path: Path, slurm_state_env):
    """Run ``render-attach`` once relying on recorded job-id state."""
    # Arrange
    _write_state(tmp_path, slurm_state_env)
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["template", "render-attach", str(slurm_yaml)])
    # Assert (return for assertion fan-out across parametrized tests)
    return result


@pytest.fixture
def render_attach_explicit_jobid_result(
    slurm_yaml: Path, tmp_path: Path, slurm_state_env
):
    """Run ``render-attach`` once with ``--job-id`` overriding recorded state."""
    # Arrange
    _write_state(tmp_path, slurm_state_env)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        main,
        ["template", "render-attach", str(slurm_yaml), "--job-id", "99999"],
    )
    # Assert (return for assertion fan-out across parametrized tests)
    return result


@pytest.fixture
def render_attach_non_slurm_result(claude_yaml: Path):
    """Run ``render-attach`` once against the non-SLURM YAML fixture."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["template", "render-attach", str(claude_yaml)])
    # Assert (return for assertion fan-out across parametrized tests)
    return result


@_SKIP_RENDER_ATTACH
class TestRenderAttach:
    def test_recorded_jobid_exit_code_is_zero(
        self, render_attach_recorded_result
    ) -> None:
        # Arrange
        result = render_attach_recorded_result
        # Act
        exit_code = result.exit_code
        # Assert
        assert exit_code == 0, result.output

    @pytest.mark.parametrize(
        "expected_fragment",
        [
            "srun --jobid=54321",
            "--pty",
            "tmux -L default attach -t head-spartan",
        ],
    )
    def test_recorded_jobid_output_contains_expected_fragment(
        self, render_attach_recorded_result, expected_fragment: str
    ) -> None:
        # Arrange
        output = render_attach_recorded_result.output
        # Act
        present = expected_fragment in output
        # Assert
        assert present, output

    def test_explicit_jobid_exit_code_is_zero(
        self, render_attach_explicit_jobid_result
    ) -> None:
        # Arrange
        result = render_attach_explicit_jobid_result
        # Act
        exit_code = result.exit_code
        # Assert
        assert exit_code == 0, result.output

    def test_explicit_jobid_appears_in_output(
        self, render_attach_explicit_jobid_result
    ) -> None:
        # Arrange
        output = render_attach_explicit_jobid_result.output
        # Act
        present = "srun --jobid=99999" in output
        # Assert
        assert present, output

    def test_explicit_jobid_suppresses_recorded_jobid(
        self, render_attach_explicit_jobid_result
    ) -> None:
        # Arrange
        output = render_attach_explicit_jobid_result.output
        # Act
        recorded_absent = "54321" not in output
        # Assert
        assert recorded_absent, output

    def test_non_slurm_runtime_exits_non_zero(
        self, render_attach_non_slurm_result
    ) -> None:
        # Arrange
        result = render_attach_non_slurm_result
        # Act
        exit_code = result.exit_code
        # Assert
        assert exit_code != 0

    def test_non_slurm_runtime_emits_requires_slurm_message(
        self, render_attach_non_slurm_result
    ) -> None:
        # Arrange
        output = render_attach_non_slurm_result.output
        # Act
        has_message = "requires runtime: slurm" in output
        # Assert
        assert has_message, output
