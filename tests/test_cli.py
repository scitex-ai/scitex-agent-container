"""Tests for the CLI commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli import main


VALID_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {"name": "cli-test"},
    "spec": {"runtime": "claude-code", "model": "sonnet"},
}


def _write_config(data: dict) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


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
        result = runner.invoke(main, ["ps"])
        assert result.exit_code == 0
        assert "No agents" in result.output

    def test_stop_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_health_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["health", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_logs_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["logs", "nonexistent-agent"])
        assert result.exit_code != 0

    def test_cleanup(self):
        runner = CliRunner()
        result = runner.invoke(main, ["cleanup"])
        assert result.exit_code == 0

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.2.0" in result.output

    def test_list_json_empty(self):
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_ps_json_empty(self):
        runner = CliRunner()
        result = runner.invoke(main, ["ps", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_status_json_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["status", "nonexistent", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert "error" in data

    def test_health_json_nonexistent(self):
        runner = CliRunner()
        result = runner.invoke(main, ["health", "nonexistent", "--json"])
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
        result = runner.invoke(main, ["list", "--capability", "gpu"])
        assert result.exit_code == 0

    def test_list_with_machine_filter(self):
        """The --machine flag should be accepted even with no agents."""
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--machine", "spartan"])
        assert result.exit_code == 0

    def test_find_in_directory(self):
        """find command should search YAML configs in a directory."""
        # Create a temp dir with a valid agent YAML that has capabilities
        config_with_caps = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {
                "name": "test-gpu-agent",
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
            path = Path(tmpdir) / "gpu-agent.yaml"
            with open(path, "w") as f:
                yaml.safe_dump(config_with_caps, f)

            runner = CliRunner()
            result = runner.invoke(main, ["find", "gpu", "--dir", tmpdir, "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["name"] == "test-gpu-agent"
            assert "gpu" in data[0]["capabilities"]

    def test_find_no_match(self):
        """find command should return empty when no agents match."""
        import tempfile
        config_no_match = {
            "apiVersion": "cld-agent/v1",
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
