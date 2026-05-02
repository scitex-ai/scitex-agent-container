"""Tests for the CLI commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli import main

VALID_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "claude-code", "model": "sonnet"},
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
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "SciTeX Agent Container" in result.output

    def test_validate_valid(self):
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", path])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()
        Path(path).unlink()

    def test_validate_invalid(self):
        data = {**VALID_CONFIG, "apiVersion": "wrong"}
        path = _write_config(data)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", path])
        assert result.exit_code != 0
        Path(path).unlink()

    def test_status_no_agents(self):
        runner = CliRunner()
        result = runner.invoke(main, ["list-agents", "--json"])
        assert result.exit_code == 0

    def test_stop_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_health_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["check-health", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_logs_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["show-logs", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_cleanup(self):
        runner = CliRunner()
        # cleanup now confirms by default; pass --yes for non-interactive runs.
        result = runner.invoke(main, ["clean-registry", "--yes"])
        assert result.exit_code == 0

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "version" in result.output

    def test_list_json_empty(self):
        runner = CliRunner()
        result = runner.invoke(main, ["list-agents", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_list_replaces_ps(self):
        """ps command was removed; list covers the same functionality."""
        runner = CliRunner()
        result = runner.invoke(main, ["ps"])
        # ps no longer exists — should fail with usage error
        assert result.exit_code != 0

    def test_status_json_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["show-status", "nonexistent", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert "error" in data

    def test_health_json_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["check-health", "nonexistent", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert "error" in data

    def test_list_python_apis(self):
        runner = CliRunner()
        result = runner.invoke(main, ["list-python-apis"])
        assert result.exit_code == 0
        assert "API tree" in result.output

    def test_list_python_apis_json(self):
        runner = CliRunner()
        result = runner.invoke(main, ["list-python-apis", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_help_recursive(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help-recursive"])
        assert result.exit_code == 0
        assert "Complete Command Reference" in result.output
        # Should list subcommands
        assert "start" in result.output
        assert "stop" in result.output
        assert "list-python-apis" in result.output

    def test_list_with_capability_filter(self):
        """The --capability flag should be accepted even with no agents."""
        runner = CliRunner()
        result = runner.invoke(main, ["list-agents", "--capability", "gpu"])
        assert result.exit_code == 0

    def test_list_with_machine_filter(self):
        """The --machine flag should be accepted even with no agents."""
        runner = CliRunner()
        result = runner.invoke(main, ["list-agents", "--machine", "spartan"])
        assert result.exit_code == 0

    def test_find_in_directory(self):
        """find command should search YAML configs in <name>/<name>.yaml dirs."""
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
            "spec": {"runtime": "claude-code", "model": "sonnet"},
        }
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir) / "test-gpu-agent"
            agent_dir.mkdir()
            path = agent_dir / "test-gpu-agent.yaml"
            with open(path, "w") as f:
                yaml.safe_dump(config_with_caps, f)

            runner = CliRunner()
            result = runner.invoke(main, ["find", "gpu", "--dir", tmpdir, "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["name"] == "test-gpu-agent"
            assert "gpu" in data[0]["capabilities"]

    def test_check_local_agent(self):
        """check command should run preflight checks on a local agent."""
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        result = runner.invoke(main, ["check", path])
        # Should succeed on a local machine that has python and screen
        # Even if screen is missing, the command itself should not crash
        assert "Checking" in result.output
        Path(path).unlink()

    def test_check_remote_agent_no_ssh(self):
        """check command on unreachable remote should fail gracefully."""
        remote_config = {
            **VALID_CONFIG,
            "spec": {
                **VALID_CONFIG["spec"],
                "remote": {
                    "host": "192.0.2.1",  # RFC 5737 TEST-NET, unreachable
                    "user": "testuser",
                },
            },
        }
        path = _write_config(remote_config)
        runner = CliRunner()
        result = runner.invoke(main, ["check", path])
        assert result.exit_code != 0
        assert "SSH connection" in result.output
        assert "FAIL" in result.output
        Path(path).unlink()

    def test_start_help_shows_no_preflight(self):
        """start --help should show the --no-preflight option."""
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--help"])
        assert result.exit_code == 0
        assert "--no-preflight" in result.output

    def test_find_no_match(self):
        """find command should return empty when no agents match."""
        import tempfile

        config_no_match = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {
                "name": "basic-agent",
                "labels": {"role": "head"},
            },
            "spec": {"runtime": "claude-code"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "basic.yaml"
            with open(path, "w") as f:
                yaml.safe_dump(config_no_match, f)

            runner = CliRunner()
            result = runner.invoke(main, ["find", "gpu", "--dir", tmpdir])
            assert result.exit_code == 0
            assert "No agents found" in result.output


# ----------------------------------------------------------------------------
# Regression tests for todo#254 — list --json must not block when SSH
# fan-out hits a timeout. Per-probe timeout + parallel fan-out keeps the
# whole list command bounded instead of 5s-timeout-blocking per-agent.
# ----------------------------------------------------------------------------


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

    def test_timeout_bound_with_hanging_remote(self, monkeypatch, tmp_path):
        """A hanging remote probe must not exceed the per-probe timeout."""
        import time

        from scitex_agent_container.cli_pkg import _helpers

        # v3 dir-as-SSoT: name comes from parent dir.
        rd = tmp_path / "test-remote"
        rd.mkdir()
        remote_cfg_path = rd / "test-remote.yaml"
        remote_cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: claude-code
  remote:
    host: fake-remote-host
    user: ywatanabe
"""
        )
        ld = tmp_path / "test-local"
        ld.mkdir()
        local_cfg_path = ld / "test-local.yaml"
        local_cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: claude-code
"""
        )

        class _FakeRegistry:
            def list_all(self):
                return [
                    {
                        "name": "test-remote",
                        "screen": "test-remote",
                        "config": str(remote_cfg_path),
                        "started_at": "?",
                    },
                    {
                        "name": "test-local",
                        "screen": "test-local",
                        "config": str(local_cfg_path),
                        "started_at": "?",
                    },
                ]

        # Patch ClaudeCodeRuntime to simulate a hang on the remote agent.
        class _HangingRuntime:
            def is_running(self, cfg):
                time.sleep(10)  # longer than our probe timeout
                return True

        monkeypatch.setattr(
            _helpers,
            "_probe_remote",
            lambda cfg: _HangingRuntime().is_running(cfg),
            raising=False,
        )

        # Patch ScreenManager for the local agent path.
        from scitex_agent_container.runtimes import screen as _screen

        monkeypatch.setattr(_screen.ScreenManager, "exists", lambda n: False)

        t0 = time.monotonic()
        # Module-level function reference — the monkeypatch above replaced
        # ``_probe_remote`` within _helpers so any call through that module
        # picks up the hanging mock.
        rows = _helpers.get_agent_list_data(_FakeRegistry(), remote_probe_timeout_s=1.0)
        elapsed = time.monotonic() - t0

        # Bound: the 10s hang must be cut short by the 1s timeout. Even
        # with ThreadPool overhead, elapsed should be < 3s (generous).
        assert elapsed < 3.0, (
            f"get_agent_list_data blocked for {elapsed:.1f}s despite 1s "
            "probe timeout — todo#254 regression re-introduced"
        )
        # And the remote row must be marked liveness_unknown, not "stopped"
        remote_row = next(r for r in rows if r["name"] == "test-remote")
        assert remote_row["status"] == "unknown"
        assert remote_row.get("liveness_unknown") is True

    def test_fast_remote_probe_not_marked_unknown(self, monkeypatch, tmp_path):
        """A fast remote probe must NOT be marked liveness_unknown."""
        from scitex_agent_container.cli_pkg import _helpers

        d = tmp_path / "test-fast"
        d.mkdir()
        cfg_path = d / "test-fast.yaml"
        cfg_path.write_text(
            """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: claude-code
  remote:
    host: fake-fast-host
    user: ywatanabe
"""
        )

        class _FakeRegistry:
            def list_all(self):
                return [
                    {
                        "name": "test-fast",
                        "screen": "test-fast",
                        "config": str(cfg_path),
                        "started_at": "?",
                    }
                ]

        monkeypatch.setattr(_helpers, "_probe_remote", lambda cfg: True, raising=False)

        rows = _helpers.get_agent_list_data(_FakeRegistry(), remote_probe_timeout_s=5.0)
        row = rows[0]
        assert row["status"] == "running"
        assert row.get("liveness_unknown") is not True
