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
    stopped: list = []
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    assert result.exit_code == 0, result.output
    assert sorted(s[0] for s in stopped) == ["a", "b"]


def test_bulk_failure_reports_and_exits_nonzero(tmp_path):
    def _boom(_name, _force):
        raise RuntimeError("boom")

    root = _seed(tmp_path, ["a", "b"])
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", _boom),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_single_name_path(tmp_path):
    stopped: list = []
    with _swap("agent_stop", lambda name, force: stopped.append((name, force))):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    assert result.exit_code == 0
    assert stopped == [("alpha", False)]
    assert "Agent 'alpha' stopped" in result.output


def test_single_yaml_path_resolves_name(tmp_path):
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    stopped: list = []
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(p)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved-foo")),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(p), "--force"])
    assert result.exit_code == 0
    assert stopped == [("resolved-foo", True)]


def test_single_failure_exits_nonzero(tmp_path):
    def _boom(name, force=False):
        raise RuntimeError("nope")

    with _swap("agent_stop", _boom):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    assert result.exit_code == 1
    assert "nope" in result.output
