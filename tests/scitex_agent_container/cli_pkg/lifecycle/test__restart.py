"""Tests for cli_pkg.lifecycle._restart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._restart import restart


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


class _FakeCfg:
    def __init__(self, name):
        self.name = name


def test_dry_run():
    runner = CliRunner()
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    assert result.exit_code == 0
    assert "would restart agent 'alpha'" in result.output


def test_refuse_without_yes():
    runner = CliRunner()
    result = runner.invoke(restart, ["alpha"])
    assert result.exit_code == 2
    assert "Refusing to restart" in result.output


def test_happy_path_name():
    called = []
    with patch(
        "scitex_agent_container.cli_pkg.lifecycle._restart.agent_restart",
        side_effect=lambda name: called.append(name),
    ):
        runner = CliRunner()
        result = runner.invoke(restart, ["alpha", "-y"])
    assert result.exit_code == 0, result.output
    assert called == ["alpha"]
    assert "restarted" in result.output


def test_yaml_path_resolves_name(tmp_path):
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    called = []
    with (
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._restart.resolve_with_prefix",
            return_value=str(p),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._restart.load_config",
            return_value=_FakeCfg("resolved"),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._restart.agent_restart",
            side_effect=lambda name: called.append(name),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(restart, [str(p), "-y"])
    assert result.exit_code == 0
    assert called == ["resolved"]


def test_failure_exits_nonzero():
    with patch(
        "scitex_agent_container.cli_pkg.lifecycle._restart.agent_restart",
        side_effect=RuntimeError("boom"),
    ):
        runner = CliRunner()
        result = runner.invoke(restart, ["alpha", "-y"])
    assert result.exit_code == 1
    assert "boom" in result.output
