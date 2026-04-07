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
        assert "0.1.0" in result.output

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
