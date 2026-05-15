"""Tests for cli_pkg.lifecycle._stop.

PA-306: no ``unittest.mock``. Collaborators are swapped at the
module namespace via a small ``_swap`` context manager.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._stop as stop_mod
from scitex_agent_container.cli_pkg.lifecycle._stop import stop


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path):
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@contextmanager
def _swap(name: str, fn: Callable) -> Iterator[None]:
    saved = getattr(stop_mod, name)
    setattr(stop_mod, name, fn)
    try:
        yield
    finally:
        setattr(stop_mod, name, saved)


def _seed(tmp_path: Path, names) -> Path:
    """Seed a directory with N agents (each <name>/<name>.yaml)."""
    root = tmp_path / "agents"
    for n in names:
        d = root / n
        d.mkdir(parents=True)
        (d / f"{n}.yaml").write_text(f"name: {n}\n")
    return root


class _FakeCfg:
    def __init__(self, name: str) -> None:
        self.name = name


# ---------------------------------------------------------------------------
# Dry-run: enumerates targets without invoking agent_stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def dry_run_result(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, [str(root), "extra", "--dry-run"])
    # Assert
    return result


def test_dry_run_lists_targets_exits_zero(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_dry_run_lists_targets_mentions_single_name(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    out = result.output
    # Assert
    assert "would stop agent 'extra'" in out


def test_dry_run_lists_targets_mentions_bulk_yaml(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    out = result.output
    # Assert
    assert "would stop agent at" in out


# ---------------------------------------------------------------------------
# Bulk-without-yes refuses.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_no_yes_result(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, [str(root)])
    # Assert
    return result


def test_bulk_without_yes_refuses_exits_two(bulk_no_yes_result):
    # Arrange
    result = bulk_no_yes_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 2


def test_bulk_without_yes_refuses_prints_message(bulk_no_yes_result):
    # Arrange
    result = bulk_no_yes_result
    # Act
    out = result.output
    # Assert
    assert "Refusing to stop 2 agents" in out


# ---------------------------------------------------------------------------
# Bulk-with-yes: invokes agent_stop for each seeded agent.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_with_yes_run(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    stopped: list = []
    # Act
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    # Assert
    return result, stopped


def test_bulk_with_yes_stops_all_exits_zero(bulk_with_yes_run):
    # Arrange
    result, _ = bulk_with_yes_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0, result.output


def test_bulk_with_yes_stops_all_invokes_agent_stop_per_agent(bulk_with_yes_run):
    # Arrange
    _, stopped = bulk_with_yes_run
    # Act
    names = sorted(s[0] for s in stopped)
    # Assert
    assert names == ["a", "b"]


# ---------------------------------------------------------------------------
# Bulk failure: continues past errors and exits nonzero.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_failure_result(tmp_path):
    # Arrange
    def _boom(_name, _force):
        raise RuntimeError("boom")

    root = _seed(tmp_path, ["a", "b"])
    # Act
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", _boom),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    # Assert
    return result


def test_bulk_failure_reports_and_exits_nonzero_exit_code(bulk_failure_result):
    # Arrange
    result = bulk_failure_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_bulk_failure_reports_and_exits_nonzero_prints_error(bulk_failure_result):
    # Arrange
    result = bulk_failure_result
    # Act
    out = result.output
    # Assert
    assert "boom" in out


# ---------------------------------------------------------------------------
# Single-target by name: forwarded straight to agent_stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_name_run():
    # Arrange
    stopped: list = []
    # Act
    with _swap("agent_stop", lambda name, force: stopped.append((name, force))):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    # Assert
    return result, stopped


def test_single_name_path_exits_zero(single_name_run):
    # Arrange
    result, _ = single_name_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_single_name_path_invokes_agent_stop_with_name(single_name_run):
    # Arrange
    _, stopped = single_name_run
    # Act
    calls = list(stopped)
    # Assert
    assert calls == [("alpha", False)]


def test_single_name_path_prints_stopped_message(single_name_run):
    # Arrange
    result, _ = single_name_run
    # Act
    out = result.output
    # Assert
    assert "Agent 'alpha' stopped" in out


# ---------------------------------------------------------------------------
# Single-target by YAML path: resolved to config.name before stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_yaml_run(tmp_path):
    # Arrange
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    stopped: list = []
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(p)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved-foo")),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(p), "--force"])
    # Assert
    return result, stopped


def test_single_yaml_path_resolves_name_exits_zero(single_yaml_run):
    # Arrange
    result, _ = single_yaml_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_single_yaml_path_resolves_name_invokes_agent_stop_with_resolved_name(
    single_yaml_run,
):
    # Arrange
    _, stopped = single_yaml_run
    # Act
    calls = list(stopped)
    # Assert
    assert calls == [("resolved-foo", True)]


# ---------------------------------------------------------------------------
# Single-target failure: exits nonzero and surfaces the error.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_failure_result():
    # Arrange
    def _boom(name, force=False):
        raise RuntimeError("nope")

    # Act
    with _swap("agent_stop", _boom):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    # Assert
    return result


def test_single_failure_exits_nonzero_exit_code(single_failure_result):
    # Arrange
    result = single_failure_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_single_failure_exits_nonzero_prints_error(single_failure_result):
    # Arrange
    result = single_failure_result
    # Act
    out = result.output
    # Assert
    assert "nope" in out
