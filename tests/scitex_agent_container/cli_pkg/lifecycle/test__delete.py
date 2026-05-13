"""Tests for cli_pkg.lifecycle._delete."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._delete import delete


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _seed_agent(tmp_path: Path, name: str, *, spec=True, runtime=True) -> None:
    root = tmp_path / ".scitex" / "agent-container"
    if spec:
        d = root / "agents" / name
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text("apiVersion: scitex-agent-container/v3\n")
    if runtime:
        rt = root / "runtime" / name
        rt.mkdir(parents=True)
        (rt / "session.jsonl").write_text("{}\n")


def test_dry_run_lists_components(tmp_path):
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        RegCls.return_value.exists.return_value = True
        result = runner.invoke(delete, ["alpha", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would delete 'alpha'" in result.output


def test_skip_when_not_found(tmp_path):
    runner = CliRunner()
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        RegCls.return_value.exists.return_value = False
        result = runner.invoke(delete, ["ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_bulk_without_yes_refuses(tmp_path):
    _seed_agent(tmp_path, "a")
    _seed_agent(tmp_path, "b")
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        RegCls.return_value.exists.return_value = True
        runner = CliRunner()
        result = runner.invoke(delete, ["a", "b"])
    assert result.exit_code == 2
    assert "Refusing to delete 2 agents" in result.output


def test_full_delete_removes_dirs_and_calls_registry(tmp_path):
    _seed_agent(tmp_path, "alpha")
    root = tmp_path / ".scitex" / "agent-container"
    spec_dir = root / "agents" / "alpha"
    rt_dir = root / "runtime" / "alpha"
    assert spec_dir.exists() and rt_dir.exists()

    removed_names = []
    stop_calls = []

    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        reg = RegCls.return_value
        reg.exists.return_value = True
        reg.remove.side_effect = lambda n: removed_names.append(n)
        with patch(
            "scitex_agent_container._lifecycle.lifecycle.agent_stop",
            side_effect=lambda yaml, force: stop_calls.append(yaml),
        ):
            runner = CliRunner()
            result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0, result.output
    assert not spec_dir.exists()
    assert not rt_dir.exists()
    assert removed_names == ["alpha"]
    assert stop_calls and stop_calls[0].endswith("spec.yaml")
    assert "deleted" in result.output


def test_keep_runtime_preserves_runtime(tmp_path):
    _seed_agent(tmp_path, "alpha")
    rt_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "alpha"
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        RegCls.return_value.exists.return_value = True
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha", "--keep-runtime"])
    assert result.exit_code == 0
    assert rt_dir.exists()


def test_rmtree_failure_reports_warn_and_exits_nonzero(tmp_path, monkeypatch):
    _seed_agent(tmp_path, "alpha")


    real_rmtree = __import__("shutil").rmtree

    def fake_rmtree(p):
        raise OSError("permission denied")

    monkeypatch.setattr("shutil.rmtree", fake_rmtree)
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        RegCls.return_value.exists.return_value = True
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 1
    assert "could not remove" in result.output


def test_stop_failure_is_swallowed(tmp_path):
    _seed_agent(tmp_path, "alpha")
    with (
        patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls,
        patch(
            "scitex_agent_container._lifecycle.lifecycle.agent_stop",
            side_effect=RuntimeError("stop failed"),
        ),
    ):
        RegCls.return_value.exists.return_value = True
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    # Stop failure is best-effort; delete still proceeds.
    assert result.exit_code == 0
    assert "deleted" in result.output


def test_registry_remove_failure_swallowed(tmp_path):
    _seed_agent(tmp_path, "alpha")
    with patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls:
        reg = RegCls.return_value
        reg.exists.return_value = True
        reg.remove.side_effect = KeyError("nope")
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0
    assert "deleted" in result.output


def test_no_spec_yaml_skips_stop(tmp_path):
    """spec dir exists, but no spec.yaml file → no agent_stop call."""
    _seed_agent(tmp_path, "alpha")
    # remove spec.yaml
    (
        tmp_path / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
    ).unlink()
    stop_calls = []
    with (
        patch("scitex_agent_container.cli_pkg.lifecycle._delete.Registry") as RegCls,
        patch(
            "scitex_agent_container._lifecycle.lifecycle.agent_stop",
            side_effect=lambda yaml, force: stop_calls.append(yaml),
        ),
    ):
        RegCls.return_value.exists.return_value = True
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0
    assert stop_calls == []
