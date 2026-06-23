"""Tests for the CLI commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli import main

VALID_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {
        "runtime": "apptainer",
        "host": "local",
        "workdir": "/home/agent/work",
        "apptainer": {"image": "/x.sif", "binds": []},
        "claude": {"model": "sonnet"},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}


def _write_config(data: dict, name: str = "cli-test") -> str:
    """Write config under <tmp>/<name>/<name>.yaml (dir-as-SSoT)."""
    import copy

    data = copy.deepcopy(data)
    metadata = data.get("metadata") or {}
    metadata.pop("name", None)
    if metadata:
        data["metadata"] = metadata
    elif "metadata" in data:
        del data["metadata"]
    tmp_dir = Path(tempfile.mkdtemp()) / name
    tmp_dir.mkdir(parents=True)
    path = tmp_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


class TestCLI:
    def test_main_help_flag_prints_program_banner(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help"])
        # Assert
        assert result.exit_code == 0 and "SciTeX Agent Container" in result.output

    def test_agents_check_with_valid_yaml_passes_validation(self):
        # Arrange
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        # check now does YAML validation first; runtime probes may fail in test
        # env (no docker), so we only assert validation passed by checking output.
        try:
            assert "validation failed" not in result.output.lower()
        finally:
            Path(path).unlink()

    def test_agents_check_with_wrong_apiversion_exits_nonzero(self):
        # Arrange
        data = {**VALID_CONFIG, "apiVersion": "wrong"}
        path = _write_config(data)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        try:
            assert result.exit_code != 0
        finally:
            Path(path).unlink()

    def test_agents_list_json_with_no_agents_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_stop_unknown_agent_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["stop", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_agents_health_unknown_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_agents_tail_unknown_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "tail", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_db_clean_sweep_exits_zero(self):
        # Arrange
        # F-CS11 phase 5: `registry clean` was renamed to `db clean`.
        # The new path is the SQLite GC sweep — runs against state.db,
        # exits 0 with zero-or-more swept entries.
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["db", "clean"])
        # Assert
        assert result.exit_code == 0

    def test_main_version_flag_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--version"])
        # Assert
        assert result.exit_code == 0

    def test_main_version_flag_prints_version_label(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--version"])
        # Assert
        assert "version" in result.output

    def test_agents_list_json_empty_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_agents_list_json_empty_returns_agents_payload(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        data = json.loads(result.output)
        assert isinstance(data, dict) and "agents" in data

    def test_removed_ps_command_exits_nonzero(self):
        """ps command was removed; list covers the same functionality."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["ps"])
        # Assert
        # ps no longer exists — should fail with usage error
        assert result.exit_code != 0

    def test_agents_list_unknown_name_json_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "nonexistent", "--json"])
        # Assert
        assert result.exit_code != 0

    def test_agents_list_unknown_name_json_emits_error_key(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "nonexistent", "--json"])
        # Assert
        data = json.loads(result.output)
        assert "error" in data

    def test_agents_health_unknown_name_json_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent", "--json"])
        # Assert
        assert result.exit_code != 0

    def test_agents_health_unknown_name_json_emits_error_key(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent", "--json"])
        # Assert
        data = json.loads(result.output)
        assert "error" in data

    def test_list_python_apis_command_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis"])
        # Assert
        assert result.exit_code == 0

    def test_list_python_apis_command_prints_api_tree(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis"])
        # Assert
        assert "API tree" in result.output

    def test_list_python_apis_json_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_list_python_apis_json_returns_nonempty_list(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis", "--json"])
        # Assert
        data = json.loads(result.output)
        assert isinstance(data, list) and len(data) > 0

    def test_help_recursive_command_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help-recursive"])
        # Assert
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "expected_substring",
        ["Complete Command Reference", "start", "stop", "list-python-apis"],
    )
    def test_help_recursive_output_contains_substring(self, expected_substring):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help-recursive"])
        # Assert
        assert expected_substring in result.output

    def test_agents_list_capability_filter_flag_accepted(self):
        """The --capability flag should be accepted even with no agents."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--capability", "gpu"])
        # Assert
        assert result.exit_code == 0

    def test_agents_list_machine_filter_flag_accepted(self):
        """The --machine flag should be accepted even with no agents."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--machine", "spartan"])
        # Assert
        assert result.exit_code == 0

    def _find_setup_gpu_agent(self, tmpdir):
        """Shared setup for agents-find tests below."""
        config_with_caps = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {
                "labels": {
                    "role": "head",
                    "machine": "spartan",
                    "capabilities": "gpu,slurm,ml-training",
                },
            },
            "spec": {
                "runtime": "apptainer",
                "host": "local",
                "workdir": "/home/agent/work",
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "sonnet"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            },
        }
        agent_dir = Path(tmpdir) / "test-gpu-agent"
        agent_dir.mkdir()
        path = agent_dir / "spec.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(config_with_caps, f)
        runner = CliRunner()
        return runner.invoke(main, ["agents", "find", "gpu", "--dir", tmpdir, "--json"])

    def test_agents_find_in_directory_exits_zero(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            assert result.exit_code == 0

    def test_agents_find_in_directory_returns_one_match(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.output)
            assert len(data) == 1

    def test_agents_find_in_directory_match_has_expected_name(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.output)
            assert data[0]["name"] == "test-gpu-agent"

    def test_agents_find_in_directory_match_has_gpu_capability(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.output)
            assert "gpu" in data[0]["capabilities"]

    def test_agents_check_local_agent_runs_preflight(self):
        """check command should run preflight checks on a local agent."""
        # Arrange
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        # Should succeed on a local machine that has python and screen
        # Even if screen is missing, the command itself should not crash
        try:
            assert "Checking" in result.output
        finally:
            Path(path).unlink()

    def test_start_help_exits_zero(self):
        """start --help should exit cleanly."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["start", "--help"])
        # Assert
        assert result.exit_code == 0

    def test_start_help_shows_no_preflight_option(self):
        """start --help should show the --no-preflight option."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["start", "--help"])
        # Assert
        assert "--no-preflight" in result.output

    def _find_setup_no_match(self, tmpdir):
        """Shared setup for agents-find no-match test."""
        config_no_match = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {
                "name": "basic-agent",
                "labels": {"role": "head"},
            },
            "spec": {"runtime": "apptainer"},
        }
        path = Path(tmpdir) / "basic.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(config_no_match, f)
        runner = CliRunner()
        return runner.invoke(main, ["agents", "find", "gpu", "--dir", tmpdir])

    def test_agents_find_no_match_exits_zero(self):
        """find command should exit 0 when no agents match."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_no_match(tmpdir)
            # Assert
            assert result.exit_code == 0

    def test_agents_find_no_match_reports_no_agents_found(self):
        """find command should print a no-match message when nothing matches."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_no_match(tmpdir)
            # Assert
            assert "No agents found" in result.output


# ----------------------------------------------------------------------------
# Regression tests for todo#254 — list --json must not block when SSH
# fan-out hits a timeout. Per-probe timeout + parallel fan-out keeps the
# whole list command bounded instead of 5s-timeout-blocking per-agent.
# ----------------------------------------------------------------------------


class _HangingRuntime:
    """Fake runtime that simulates a hung probe (todo#254 regression)."""

    def is_running(self, cfg):
        import time

        time.sleep(10)  # longer than the probe timeout
        return True


class _FakeRegistryTwoAgents:
    def __init__(self, remote_cfg_path, local_cfg_path):
        self._remote_cfg_path = remote_cfg_path
        self._local_cfg_path = local_cfg_path

    def list_all(self):
        return [
            {
                "name": "test-remote",
                "screen": "test-remote",
                "config": str(self._remote_cfg_path),
                "started_at": "?",
            },
            {
                "name": "test-local",
                "screen": "test-local",
                "config": str(self._local_cfg_path),
                "started_at": "?",
            },
        ]


class _FakeRegistryOneAgent:
    def __init__(self, cfg_path):
        self._cfg_path = cfg_path

    def list_all(self):
        return [
            {
                "name": "test-fast",
                "screen": "test-fast",
                "config": str(self._cfg_path),
                "started_at": "?",
            }
        ]


class TestListJsonTimeoutBudget:
    """todo#254 regression suite.

    Pre-fix: a hung SSH probe for one remote agent would block the entire
    `scitex-agent-container list --json` command past its 5s smoke-test
    budget, because ClaudeCodeRuntime().is_running(cfg) was called serially
    per agent in get_agent_list_data.

    Post-fix: each remote probe has a per-probe timeout (default 2s) and
    is run in a ThreadPoolExecutor. Probes that time out produce
    status="unknown" + liveness_unknown=True in the output row.
    """

    @staticmethod
    def _run_hanging_probe(tmp_path):
        import time

        from scitex_agent_container.cli_pkg import _helpers

        # v3 dir-as-SSoT: name comes from parent dir.
        rd = tmp_path / "test-remote"
        rd.mkdir()
        remote_cfg_path = rd / "test-remote.yaml"
        # v3-realign: spec.remote removed; the agent-list-data probe still
        # treats agents without a runtime PID as "remote-ish" (liveness
        # unknown) — that's what this test exercises, so a minimal v3
        # spec without spec.remote is sufficient.
        remote_cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
"""
        )
        ld = tmp_path / "test-local"
        ld.mkdir()
        local_cfg_path = ld / "test-local.yaml"
        local_cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
"""
        )

        # PA-306: hand-rolled fake injection with explicit restore.
        saved_probe = getattr(_helpers, "_probe_local", None)
        _helpers._probe_local = lambda cfg: _HangingRuntime().is_running(cfg)
        try:
            t0 = time.monotonic()
            rows = _helpers.get_agent_list_data(
                _FakeRegistryTwoAgents(remote_cfg_path, local_cfg_path),
                remote_probe_timeout_s=1.0,
            )
            elapsed = time.monotonic() - t0
        finally:
            if saved_probe is None:
                if hasattr(_helpers, "_probe_local"):
                    delattr(_helpers, "_probe_local")
            else:
                _helpers._probe_local = saved_probe
        return elapsed, rows

    def test_hanging_remote_probe_respects_timeout_budget(self, tmp_path):
        """A hanging remote probe must not exceed the per-probe timeout."""
        # Arrange
        probe = self._run_hanging_probe
        # Act
        elapsed, _rows = probe(tmp_path)
        # Assert
        # Bound: the 10s hang must be cut short by the 1s timeout.
        assert elapsed < 3.0, (
            f"get_agent_list_data blocked for {elapsed:.1f}s despite 1s "
            "probe timeout — todo#254 regression re-introduced"
        )

    def test_hanging_remote_probe_marks_status_unknown(self, tmp_path):
        """A timed-out remote probe must produce status='unknown'."""
        # Arrange
        probe = self._run_hanging_probe
        # Act
        _elapsed, rows = probe(tmp_path)
        # Assert
        remote_row = next(r for r in rows if r["name"] == "test-remote")
        assert remote_row["status"] == "unknown"

    def test_hanging_remote_probe_marks_liveness_unknown_true(self, tmp_path):
        """A timed-out remote probe must set liveness_unknown=True."""
        # Arrange
        probe = self._run_hanging_probe
        # Act
        _elapsed, rows = probe(tmp_path)
        # Assert
        remote_row = next(r for r in rows if r["name"] == "test-remote")
        assert remote_row.get("liveness_unknown") is True

    @staticmethod
    def _run_fast_probe(tmp_path):
        from scitex_agent_container.cli_pkg import _helpers

        d = tmp_path / "test-fast"
        d.mkdir()
        cfg_path = d / "test-fast.yaml"
        cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: local
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: sonnet
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
"""
        )

        # PA-306: hand-rolled fake injection with explicit restore.
        saved_probe = getattr(_helpers, "_probe_local", None)
        _helpers._probe_local = lambda cfg: True
        try:
            rows = _helpers.get_agent_list_data(
                _FakeRegistryOneAgent(cfg_path), remote_probe_timeout_s=5.0
            )
        finally:
            if saved_probe is None:
                if hasattr(_helpers, "_probe_local"):
                    delattr(_helpers, "_probe_local")
            else:
                _helpers._probe_local = saved_probe
        return rows[0]

    def test_fast_remote_probe_reports_running_status(self, tmp_path):
        """A fast remote probe must report status='running'."""
        # Arrange
        probe = self._run_fast_probe
        # Act
        row = probe(tmp_path)
        # Assert
        assert row["status"] == "running"

    def test_fast_remote_probe_is_not_marked_liveness_unknown(self, tmp_path):
        """A fast remote probe must NOT be marked liveness_unknown."""
        # Arrange
        probe = self._run_fast_probe
        # Act
        row = probe(tmp_path)
        # Assert
        assert row.get("liveness_unknown") is not True
