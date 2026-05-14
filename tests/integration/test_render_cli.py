"""Tests for ``sac render-sbatch`` and ``sac render-attach`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main

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


@pytest.mark.skip(
    reason=(
        "F-CS17: SLURM rendering is slated for deletion. The validator "
        "now hard-errors on runtime: slurm; the render_sbatch / "
        "render_attach helpers go in F-CS17 stage 3 alongside this "
        "test class."
    )
)
class TestRenderSbatch:
    def test_emits_hardened_sbatch_text(self, slurm_yaml: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["template", "render-sbatch", str(slurm_yaml)])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("#!/bin/bash\n")
        assert "#SBATCH --partition=sapphire" in result.output
        assert "set -euo pipefail" in result.output
        assert "tail -f /dev/null" in result.output
        assert "trap _sac_slurm_walltime_handler USR1" in result.output
        # Hooks declared in YAML appear in the emitted script.
        assert "/path/to/pre-agent.sh" in result.output
        assert "/path/to/walltime-notify.sh" in result.output

    def test_rejects_non_slurm_runtime(self, claude_yaml: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["template", "render-sbatch", str(claude_yaml)])
        assert result.exit_code != 0
        assert "requires runtime: slurm" in result.output


@pytest.mark.skip(
    reason=(
        "F-CS17: SLURM rendering is slated for deletion. render_attach "
        "/ render_sbatch helpers + the slurm runtime go in F-CS17 stage 3."
    )
)
class TestRenderAttach:
    def test_emits_srun_pty_command_with_recorded_jobid(
        self, slurm_yaml: Path, tmp_path: Path, monkeypatch
    ) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "54321"})
        )
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR", str(state_dir))

        runner = CliRunner()
        result = runner.invoke(main, ["template", "render-attach", str(slurm_yaml)])
        assert result.exit_code == 0, result.output
        assert "srun --jobid=54321" in result.output
        assert "--pty" in result.output
        assert "tmux -L default attach -t head-spartan" in result.output

    def test_explicit_job_id_flag_wins(
        self, slurm_yaml: Path, tmp_path: Path, monkeypatch
    ) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "54321"})
        )
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR", str(state_dir))

        runner = CliRunner()
        result = runner.invoke(
            main, ["template", "render-attach", str(slurm_yaml), "--job-id", "99999"]
        )
        assert result.exit_code == 0, result.output
        assert "srun --jobid=99999" in result.output
        assert "54321" not in result.output

    def test_rejects_non_slurm_runtime(self, claude_yaml: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["template", "render-attach", str(claude_yaml)])
        assert result.exit_code != 0
        assert "requires runtime: slurm" in result.output
