"""Tests for cli_pkg.lifecycle._restart.

PA-306: no ``unittest.mock``. Production collaborators
(``agent_restart``, ``resolve_with_prefix``, ``load_config``) are
swapped at the module's namespace via a small ``_swap`` context
manager with explicit save/restore.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._restart as restart_mod
from scitex_agent_container.cli_pkg.lifecycle._restart import restart


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
    saved = getattr(restart_mod, name)
    setattr(restart_mod, name, fn)
    try:
        yield
    finally:
        setattr(restart_mod, name, saved)


class _FakeCfg:
    def __init__(self, name: str) -> None:
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
    called: list[str] = []
    with _swap("agent_restart", lambda name: called.append(name)):
        runner = CliRunner()
        result = runner.invoke(restart, ["alpha", "-y"])
    assert result.exit_code == 0, result.output
    assert called == ["alpha"]
    assert "restarted" in result.output


def test_yaml_path_resolves_name(tmp_path):
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    called: list[str] = []
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(p)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner = CliRunner()
        result = runner.invoke(restart, [str(p), "-y"])
    assert result.exit_code == 0
    assert called == ["resolved"]


def test_failure_exits_nonzero():
    def _boom(_name: Any) -> None:
        raise RuntimeError("boom")

    with _swap("agent_restart", _boom):
        runner = CliRunner()
        result = runner.invoke(restart, ["alpha", "-y"])
    assert result.exit_code == 1
    assert "boom" in result.output
