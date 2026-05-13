"""Tests for cli_pkg.lifecycle._stop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._stop import stop


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _seed(tmp_path: Path, names) -> Path:
    """Seed a directory with N agents (each <name>/<name>.yaml)."""
    root = tmp_path / "agents"
    for n in names:
        d = root / n
        d.mkdir(parents=True)
        (d / f"{n}.yaml").write_text(f"name: {n}\n")
    return root


class _FakeCfg:
    def __init__(self, name):
        self.name = name


def test_dry_run_lists_targets(tmp_path):
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    result = runner.invoke(stop, [str(root), "extra", "--dry-run"])
    assert result.exit_code == 0
    out = result.output
    assert "would stop agent 'extra'" in out
    assert "would stop agent at" in out


def test_bulk_without_yes_refuses(tmp_path):
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    result = runner.invoke(stop, [str(root)])
    assert result.exit_code == 2
    assert "Refusing to stop 2 agents" in result.output


def test_bulk_with_yes_stops_all(tmp_path):
    root = _seed(tmp_path, ["a", "b"])
    stopped = []
    with (
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.load_config",
            side_effect=lambda p: _FakeCfg(Path(p).stem),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.agent_stop",
            side_effect=lambda name, force: stopped.append((name, force)),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    assert result.exit_code == 0, result.output
    assert sorted(s[0] for s in stopped) == ["a", "b"]


def test_bulk_failure_reports_and_exits_nonzero(tmp_path):
    root = _seed(tmp_path, ["a", "b"])
    with (
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.load_config",
            side_effect=lambda p: _FakeCfg(Path(p).stem),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.agent_stop",
            side_effect=RuntimeError("boom"),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_single_name_path(tmp_path):
    stopped = []
    with patch(
        "scitex_agent_container.cli_pkg.lifecycle._stop.agent_stop",
        side_effect=lambda name, force: stopped.append((name, force)),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    assert result.exit_code == 0
    assert stopped == [("alpha", False)]
    assert "Agent 'alpha' stopped" in result.output


def test_single_yaml_path_resolves_name(tmp_path):
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    stopped = []
    with (
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.resolve_with_prefix",
            return_value=str(p),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.load_config",
            return_value=_FakeCfg("resolved-foo"),
        ),
        patch(
            "scitex_agent_container.cli_pkg.lifecycle._stop.agent_stop",
            side_effect=lambda name, force: stopped.append((name, force)),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(p), "--force"])
    assert result.exit_code == 0
    assert stopped == [("resolved-foo", True)]


def test_single_failure_exits_nonzero(tmp_path):
    with patch(
        "scitex_agent_container.cli_pkg.lifecycle._stop.agent_stop",
        side_effect=RuntimeError("nope"),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    assert result.exit_code == 1
    assert "nope" in result.output
