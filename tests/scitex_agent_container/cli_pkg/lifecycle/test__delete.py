"""Tests for cli_pkg.lifecycle._delete.

PA-306: no ``unittest.mock`` and no ``monkeypatch``. Production
collaborators (``Registry``, ``agent_stop``, ``shutil.rmtree``) are
swapped at the module namespace via small context managers with
explicit save/restore.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container._lifecycle.lifecycle as lifecycle_mod
import scitex_agent_container.cli_pkg.lifecycle._delete as delete_mod
from scitex_agent_container.cli_pkg.lifecycle._delete import delete


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


class _FakeRegistry:
    def __init__(self, *, exists: bool = True, remove_raises: Exception | None = None):
        self._exists = exists
        self._remove_raises = remove_raises
        self.remove_calls: list[str] = []

    def exists(self, _name: str) -> bool:
        return self._exists

    def remove(self, name: str) -> None:
        if self._remove_raises is not None:
            raise self._remove_raises
        self.remove_calls.append(name)


@contextmanager
def _swap_registry(reg: _FakeRegistry) -> Iterator[_FakeRegistry]:
    saved = delete_mod.Registry
    delete_mod.Registry = lambda: reg  # type: ignore[assignment]
    try:
        yield reg
    finally:
        delete_mod.Registry = saved  # type: ignore[assignment]


@contextmanager
def _swap_agent_stop(fn: Callable) -> Iterator[None]:
    saved = lifecycle_mod.agent_stop
    lifecycle_mod.agent_stop = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        lifecycle_mod.agent_stop = saved  # type: ignore[assignment]


@contextmanager
def _swap_rmtree(fn: Callable) -> Iterator[None]:
    saved = shutil.rmtree
    shutil.rmtree = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        shutil.rmtree = saved  # type: ignore[assignment]


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
    with _swap_registry(_FakeRegistry(exists=True)):
        result = runner.invoke(delete, ["alpha", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would delete 'alpha'" in result.output


def test_skip_when_not_found(tmp_path):
    runner = CliRunner()
    with _swap_registry(_FakeRegistry(exists=False)):
        result = runner.invoke(delete, ["ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_bulk_without_yes_refuses(tmp_path):
    _seed_agent(tmp_path, "a")
    _seed_agent(tmp_path, "b")
    with _swap_registry(_FakeRegistry(exists=True)):
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

    stop_calls: list[str] = []
    reg = _FakeRegistry(exists=True)
    with (
        _swap_registry(reg),
        _swap_agent_stop(lambda yaml, force: stop_calls.append(yaml)),
    ):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0, result.output
    assert not spec_dir.exists()
    assert not rt_dir.exists()
    assert reg.remove_calls == ["alpha"]
    assert stop_calls and stop_calls[0].endswith("spec.yaml")
    assert "deleted" in result.output


def test_keep_runtime_preserves_runtime(tmp_path):
    _seed_agent(tmp_path, "alpha")
    rt_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "alpha"
    with _swap_registry(_FakeRegistry(exists=True)):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha", "--keep-runtime"])
    assert result.exit_code == 0
    assert rt_dir.exists()


def test_rmtree_failure_reports_warn_and_exits_nonzero(tmp_path):
    _seed_agent(tmp_path, "alpha")

    def fake_rmtree(_p: Any) -> None:
        raise OSError("permission denied")

    with _swap_rmtree(fake_rmtree), _swap_registry(_FakeRegistry(exists=True)):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 1
    assert "could not remove" in result.output


def test_stop_failure_is_swallowed(tmp_path):
    _seed_agent(tmp_path, "alpha")

    def _boom(_yaml, _force):
        raise RuntimeError("stop failed")

    with _swap_registry(_FakeRegistry(exists=True)), _swap_agent_stop(_boom):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0
    assert "deleted" in result.output


def test_registry_remove_failure_swallowed(tmp_path):
    _seed_agent(tmp_path, "alpha")
    with _swap_registry(_FakeRegistry(exists=True, remove_raises=KeyError("nope"))):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0
    assert "deleted" in result.output


def test_no_spec_yaml_skips_stop(tmp_path):
    """spec dir exists, but no spec.yaml file → no agent_stop call."""
    _seed_agent(tmp_path, "alpha")
    (
        tmp_path / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
    ).unlink()
    stop_calls: list[str] = []
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: stop_calls.append(yaml)),
    ):
        runner = CliRunner()
        result = runner.invoke(delete, ["alpha"])
    assert result.exit_code == 0
    assert stop_calls == []
