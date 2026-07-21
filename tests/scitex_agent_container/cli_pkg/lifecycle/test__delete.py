"""Tests for cli_pkg.lifecycle._delete.

PA-306: no ``unittest.mock`` and no ``monkeypatch``. Production
collaborators (``Registry``, ``agent_stop``, ``shutil.rmtree``) are
swapped at the module namespace via small context managers with
explicit save/restore.

TQ cleanup: module docstring summarises intent (TQ001), every test
carries AAA markers (TQ002), test names spell out the behaviour being
verified (TQ003-compatible), and each test asserts exactly one fact
(TQ007). Same-shape invariants collapse into ``pytest.parametrize``.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

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

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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
        (d / "spec.yaml").write_text(explicitize_yaml("apiVersion: scitex-agent-container/v3\n"))
    if runtime:
        rt = root / "runtime" / name
        rt.mkdir(parents=True)
        (rt / "session.jsonl").write_text("{}\n")


# ---------------------------------------------------------------------------
# Dry-run path
# ---------------------------------------------------------------------------


def test_dry_run_exits_with_zero_status_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)):
        result = runner.invoke(delete, ["alpha", "--dry-run"])
    # Assert
    assert result.exit_code == 0, result.output


def test_dry_run_announces_target_agent_in_output(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)):
        result = runner.invoke(delete, ["alpha", "--dry-run"])
    # Assert
    assert "would delete 'alpha'" in result.output


# ---------------------------------------------------------------------------
# Not-found / refuse paths
# ---------------------------------------------------------------------------


def test_unknown_agent_exits_with_nonzero_status_code(tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        result = runner.invoke(delete, ["ghost"])
    # Assert
    assert result.exit_code == 1


def test_unknown_agent_message_says_not_found(tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        result = runner.invoke(delete, ["ghost"])
    # Assert
    assert "not found" in result.output


def test_bulk_delete_without_yes_exits_with_status_two(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "a")
    _seed_agent(tmp_path, "b")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)):
        result = runner.invoke(delete, ["a", "b"])
    # Assert
    assert result.exit_code == 2


def test_bulk_delete_without_yes_reports_refusal_message(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "a")
    _seed_agent(tmp_path, "b")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)):
        result = runner.invoke(delete, ["a", "b"])
    # Assert
    assert "Refusing to delete 2 agents" in result.output


# ---------------------------------------------------------------------------
# Full-delete happy path — split into one-assertion tests
# ---------------------------------------------------------------------------


def test_full_delete_exits_with_zero_status_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("subdir", ["agents", "runtime"])
def test_full_delete_removes_per_agent_directory(tmp_path, subdir):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    target = tmp_path / ".scitex" / "agent-container" / subdir / "alpha"
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        runner.invoke(delete, ["alpha"])
    # Assert
    assert not target.exists()


def test_full_delete_invokes_registry_remove_with_agent_name(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    reg = _FakeRegistry(exists=True)
    runner = CliRunner()
    # Act
    with (
        _swap_registry(reg),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        runner.invoke(delete, ["alpha"])
    # Assert
    assert reg.remove_calls == ["alpha"]


def test_full_delete_invokes_agent_stop_with_spec_yaml_path(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    stop_calls: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: stop_calls.append(yaml)),
    ):
        runner.invoke(delete, ["alpha"])
    # Assert
    assert stop_calls and stop_calls[0].endswith("spec.yaml")


def test_full_delete_emits_deleted_marker_in_output(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert "deleted" in result.output


# ---------------------------------------------------------------------------
# --keep-runtime
# ---------------------------------------------------------------------------


def test_keep_runtime_preserves_runtime_directory(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    rt_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "alpha"
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha", "--keep-runtime"])
    # Assert
    assert rt_dir.exists(), result.output


def test_keep_runtime_still_exits_with_zero_status_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha", "--keep-runtime"])
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# rmtree failure path
# ---------------------------------------------------------------------------


def _raise_oserror(_p: Any) -> None:
    raise OSError("permission denied")


def test_rmtree_failure_exits_with_nonzero_status_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_rmtree(_raise_oserror),
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert result.exit_code == 1


def test_rmtree_failure_emits_warn_marker_in_output(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_rmtree(_raise_oserror),
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert "could not remove" in result.output


# ---------------------------------------------------------------------------
# Collaborator-failure resilience
# ---------------------------------------------------------------------------


def _boom_stop(_yaml, _force):
    raise RuntimeError("stop failed")


def test_stop_failure_does_not_break_delete_exit_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)), _swap_agent_stop(_boom_stop):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert result.exit_code == 0


def test_stop_failure_still_emits_deleted_marker_in_output(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=True)), _swap_agent_stop(_boom_stop):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert "deleted" in result.output


def test_registry_remove_failure_does_not_break_exit_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True, remove_raises=KeyError("nope"))),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert result.exit_code == 0


def test_registry_remove_failure_still_emits_deleted_marker(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True, remove_raises=KeyError("nope"))),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert "deleted" in result.output


# ---------------------------------------------------------------------------
# Missing spec.yaml path
# ---------------------------------------------------------------------------


def test_missing_spec_yaml_skips_agent_stop_invocation(tmp_path):
    # Arrange — spec dir exists but spec.yaml file does not.
    _seed_agent(tmp_path, "alpha")
    (
        tmp_path / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
    ).unlink()
    stop_calls: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: stop_calls.append(yaml)),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert stop_calls == [], result.output


def test_missing_spec_yaml_still_exits_with_zero_status_code(tmp_path):
    # Arrange
    _seed_agent(tmp_path, "alpha")
    (
        tmp_path / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
    ).unlink()
    runner = CliRunner()
    # Act
    with (
        _swap_registry(_FakeRegistry(exists=True)),
        _swap_agent_stop(lambda yaml, force: None),
    ):
        result = runner.invoke(delete, ["alpha"])
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Cross-host delete: state.db row on peer → ssh stop + rm + close row.
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_delete_env(tmp_path):
    """State.db redirect + peer config + ssh shim for cross-host delete."""
    import importlib
    import json
    import sys

    db = tmp_path / "state.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n  peer-x:\n    ssh: peer-x\n"
    )
    saved_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host = os.environ.get("SAC_HOST")
    saved_cfg = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    saved_path = os.environ.get("PATH", "")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SAC_HOST"] = "lead-host"
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg)
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "ssh.argv.jsonl"
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="zeta", host="peer-x", a2a_port=18888)
    try:
        yield {"tmp": tmp_path, "bin": bin_dir, "log": log}
    finally:
        for k, v in (
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db),
            ("SAC_HOST", saved_host),
            ("SCITEX_AGENT_CONTAINER_CONFIG", saved_cfg),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ["PATH"] = saved_path
        importlib.reload(_state_db_mod)


def _ssh_invocations_delete(log):
    import json as _json

    if not log.exists():
        return []
    return [_json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def test_cross_host_delete_exits_zero(cross_host_delete_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        result = runner.invoke(delete, ["zeta"])
    # Assert
    assert result.exit_code == 0, result.output


def test_cross_host_delete_ssh_includes_stop_and_rm(cross_host_delete_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        runner.invoke(delete, ["zeta"])
    flat = [" ".join(a) for a in _ssh_invocations_delete(cross_host_delete_env["log"])]
    # Assert
    assert any("sac agents stop zeta" in ln for ln in flat) and any(
        "rm -rf" in ln for ln in flat
    )


def test_cross_host_delete_closes_lead_side_row(cross_host_delete_env):
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        runner.invoke(delete, ["zeta"])
    rows = [r for r in list_active_instances() if r["name"] == "zeta"]
    # Assert — row closed (exit_reason=deleted, not in active list).
    assert rows == []


def test_cross_host_delete_emits_remote_marker(cross_host_delete_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        result = runner.invoke(delete, ["zeta"])
    # Assert
    assert "removed 'zeta' on peer 'peer-x'" in result.output


def test_cross_host_delete_dry_run_does_not_invoke_ssh(cross_host_delete_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap_registry(_FakeRegistry(exists=False)):
        runner.invoke(delete, ["zeta", "--dry-run"])
    # Assert
    assert _ssh_invocations_delete(cross_host_delete_env["log"]) == []
