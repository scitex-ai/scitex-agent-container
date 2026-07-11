"""Tests for ``sac agents status --workdir-audit`` (F-CS8 surface).

Real-FS fixture under tmp_path; the CLI registers a known agent via
``_state.registry`` and we invoke the status command via ``CliRunner``,
asserting the JSON output carries the F-CS8 audit fields. No mocks.

PA-306 / PA-307: env mutation via ``env_save_restore`` (NOT
monkeypatch), AAA markers, one assert per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.status_cmds import status as _status

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _populate_subdir(parent: Path, rel: str, file_count: int) -> None:
    target = parent / rel
    target.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        (target / f"f{i}").write_bytes(b"x")


@pytest.fixture(autouse=True)
def _sandbox_home(env_save_restore, tmp_path: Path):
    """Redirect ``$HOME`` so registry/state writes land in tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _register_agent_with_workdir(name: str, workdir: Path) -> Path:
    """Write a minimal agent spec.yaml under $HOME/.scitex/... and register."""
    import yaml

    home = Path.home()
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "apptainer",
            "host": "${HOSTNAME}",
            "workdir": str(workdir),
            "apptainer": {"image": "/x.sif", "binds": []},
            "claude": {"model": "sonnet"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        },
    }
    spec_path = agents_dir / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec))

    # Register in the registry so `agent_status` can find it.
    from scitex_agent_container._state.registry import Registry

    reg = Registry()
    reg.add(name=name, config_path=str(spec_path), screen_name=name)
    return spec_path


# ---------------------------------------------------------------------------
# --workdir-audit flag
# ---------------------------------------------------------------------------


def test_workdir_audit_flag_emits_audit_key(tmp_path: Path):
    # Arrange — register an agent whose workdir has a tiny .claude/ tree.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".claude").mkdir()
    (workdir / ".claude" / "x.md").write_text("x")
    _register_agent_with_workdir("flag-emits-audit", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["flag-emits-audit", "--json", "--workdir-audit"])
    payload = json.loads(result.stdout)
    # Assert
    assert "workdir_audit" in payload


def test_workdir_audit_flag_reports_file_count(tmp_path: Path):
    # Arrange — drop 7 known files in the workdir's .claude/ tree.
    workdir = tmp_path / "wd-files"
    workdir.mkdir()
    _populate_subdir(workdir / ".claude", "skills", 7)
    _register_agent_with_workdir("files-counted", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["files-counted", "--json", "--workdir-audit"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["workdir_audit"]["files"] == 7


def test_workdir_audit_flag_lists_bloat_source(tmp_path: Path, env_save_restore):
    # Arrange — push the worktrees subdir over the bloat threshold so the
    # audit lists it by relative path.
    env_save_restore.set("SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES", "3")
    workdir = tmp_path / "wd-bloat"
    workdir.mkdir()
    _populate_subdir(workdir / ".claude", "worktrees", 5)
    _register_agent_with_workdir("bloat-listed", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["bloat-listed", "--json", "--workdir-audit"])
    payload = json.loads(result.stdout)
    rel_paths = [s["rel_path"] for s in payload["workdir_audit"]["bloat_sources"]]
    # Assert
    assert "worktrees" in rel_paths


def test_workdir_audit_flag_flags_exceeded_when_over_threshold(
    tmp_path: Path, env_save_restore
):
    # Arrange — drop the file threshold low enough that fixture trips it.
    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_FILES", "3")
    workdir = tmp_path / "wd-exceeded"
    workdir.mkdir()
    _populate_subdir(workdir / ".claude", "hooks", 10)
    _register_agent_with_workdir("exceeded-flagged", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["exceeded-flagged", "--json", "--workdir-audit"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["workdir_audit"]["exceeded_files"] is True


def test_workdir_audit_flag_omits_audit_key_when_not_requested(
    tmp_path: Path,
):
    # Arrange — same workdir but no `--workdir-audit` flag; the audit
    # key must NOT appear (audit is an opt-in cost on the file walk).
    workdir = tmp_path / "wd-noflag"
    workdir.mkdir()
    (workdir / ".claude").mkdir()
    _register_agent_with_workdir("noflag", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["noflag", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "workdir_audit" not in payload


def test_workdir_audit_flag_marks_missing_when_no_claude_subtree(
    tmp_path: Path,
):
    # Arrange — workdir exists but has no .claude/ subdir at all. The
    # audit should still surface (so dashboards see the agent's status),
    # but flagged as `missing=True` rather than zero-valued silently.
    workdir = tmp_path / "wd-nopath"
    workdir.mkdir()
    _register_agent_with_workdir("nopath", workdir)
    runner = CliRunner()
    # Act
    result = runner.invoke(_status, ["nopath", "--json", "--workdir-audit"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["workdir_audit"]["missing"] is True
