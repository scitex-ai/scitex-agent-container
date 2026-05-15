"""Tests for ``cli_pkg.lifecycle._restart.restart`` (Click command).

The command has four behavioural branches: ``--dry-run`` prints a
"would restart" line without invoking the restart collaborator; absence
of ``-y``/``--yes`` refuses with exit code ``2``; the happy path
delegates to ``agent_restart(name)`` and reports success; a YAML path
argument is resolved through ``resolve_with_prefix`` / ``load_config``
so the resolved ``config.name`` is forwarded to ``agent_restart`` rather
than the raw path; any exception from ``agent_restart`` surfaces as
exit code ``1`` with the message preserved on stderr/stdout.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Production collaborators
(``agent_restart``, ``resolve_with_prefix``, ``load_config``) are
swapped at the module's namespace via a small ``_swap`` context
manager with explicit save/restore.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape invariants over a single arrange/act collapse into
``pytest.parametrize``.
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


# ---------------------------------------------------------------------------
# --dry-run branch: no collaborator invocation, prints "would restart"
# ---------------------------------------------------------------------------


def test_dry_run_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_dry_run_announces_target_agent():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert "would restart agent 'alpha'" in result.output


def test_dry_run_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Confirmation guard: missing --yes refuses with exit code 2
# ---------------------------------------------------------------------------


def test_refuse_without_yes_exits_two():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert result.exit_code == 2


def test_refuse_without_yes_emits_refusal_message():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert "Refusing to restart" in result.output


def test_refuse_without_yes_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Happy path: bare name is forwarded to agent_restart verbatim
# ---------------------------------------------------------------------------


def test_happy_path_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_happy_path_forwards_name_to_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert called == ["alpha"]


def test_happy_path_reports_success():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "restarted" in result.output


# ---------------------------------------------------------------------------
# YAML path argument: resolved through resolve_with_prefix/load_config,
# resolved config.name (not the raw path) is forwarded to agent_restart.
# ---------------------------------------------------------------------------


@pytest.fixture
def _yaml_path(tmp_path):
    path = tmp_path / "foo.yaml"
    path.write_text("name: foo\n")
    return path


def test_yaml_path_exits_zero(_yaml_path):
    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda _name: None),
    ):
        result = runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_yaml_path_forwards_resolved_name(_yaml_path):
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert called == ["resolved"]


# ---------------------------------------------------------------------------
# Failure path: any exception from agent_restart surfaces as exit code 1
# and the message is reported back to the user.
# ---------------------------------------------------------------------------


def _boom(_name: Any) -> None:
    raise RuntimeError("boom")


def test_failure_exits_one():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 1


def test_failure_reports_exception_message():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "boom" in result.output
