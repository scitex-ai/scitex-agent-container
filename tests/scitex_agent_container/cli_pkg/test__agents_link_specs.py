from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_link_specs import (
    apply_link_plans,
    build_link_plans,
    link_specs,
)


def _agent(root: Path, name: str, text: str) -> Path:
    path = root / name
    (path / "to_home").mkdir(parents=True)
    (path / "spec.yaml").write_text(text)
    (path / "to_home" / ".mcp.json").write_text('{"mcpServers": {}}\n')
    return path


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=SAC Test",
            "-c",
            "user.email=sac@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )


def _capture_exception(operation) -> BaseException:
    try:
        operation()
    except BaseException as error:
        return error
    raise AssertionError("operation did not raise")


def _other_device_or_skip(path: Path) -> Path:
    candidate = Path("/dev/shm")
    if not candidate.is_dir() or candidate.stat().st_dev == path.stat().st_dev:
        pytest.skip("test host exposes no distinct writable filesystem")
    return candidate


def test_current_link_is_an_idempotent_plan(tmp_path: Path) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    target_root.mkdir()
    (target_root / "alpha").symlink_to(source, target_is_directory=True)

    # Act
    plans = build_link_plans(
        source_root=source_root,
        target_root=target_root,
        names=("alpha",),
        backup_root=tmp_path / "backup",
    )
    apply_link_plans(plans)

    # Assert
    assert (
        plans[0].state,
        plans[0].backup,
        (target_root / "alpha").resolve(),
    ) == ("current", None, source.resolve())


def test_cli_is_dry_run_by_default_and_reports_git_identity(tmp_path: Path) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    old = _agent(target_root, "alpha", "old: true\n")
    _git_repo(source_root)

    # Act
    result = CliRunner().invoke(
        link_specs,
        [
            "--source",
            str(source_root),
            "--target",
            str(target_root),
            "--only",
            "alpha",
            "--json",
        ],
    )
    payload = json.loads(result.output)

    # Assert
    assert {
        "exit_code": result.exit_code,
        "mode": payload["mode"],
        "ok": payload["ok"],
        "has_head": bool(payload["git"]["head"]),
        "source_dirty": payload["git"]["source_dirty"],
        "state": payload["agents"][0]["state"],
        "target_is_link": old.is_symlink(),
        "target_text": (old / "spec.yaml").read_text(),
    } == {
        "exit_code": 1,
        "mode": "dry-run",
        "ok": False,
        "has_head": True,
        "source_dirty": False,
        "state": "archive_and_link",
        "target_is_link": False,
        "target_text": "old: true\n",
    }, result.output


def test_apply_archives_real_tree_then_installs_absolute_link(tmp_path: Path) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    backup_root = tmp_path / "backup"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    _agent(target_root, "alpha", "old: true\n")
    _git_repo(source_root)

    # Act
    result = CliRunner().invoke(
        link_specs,
        [
            "--source",
            str(source_root),
            "--target",
            str(target_root),
            "--backup-root",
            str(backup_root),
            "--only",
            "alpha",
            "--apply",
            "--json",
        ],
    )
    live = target_root / "alpha"

    # Assert
    assert {
        "exit_code": result.exit_code,
        "live_is_link": live.is_symlink(),
        "link_target": os.readlink(live),
        "backup_text": (backup_root / "alpha" / "spec.yaml").read_text(),
    } == {
        "exit_code": 0,
        "live_is_link": True,
        "link_target": str(source.resolve()),
        "backup_text": "old: true\n",
    }, result.output


def test_apply_refuses_uncommitted_selected_source(tmp_path: Path) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    _git_repo(source_root)
    (source / "spec.yaml").write_text("changed: true\n")

    # Act
    result = CliRunner().invoke(
        link_specs,
        [
            "--source",
            str(source_root),
            "--target",
            str(target_root),
            "--only",
            "alpha",
            "--apply",
        ],
    )

    # Assert
    assert {
        "exit_code": result.exit_code,
        "diagnostic_present": "uncommitted changes" in result.output,
        "target_exists": (target_root / "alpha").exists(),
    } == {
        "exit_code": 1,
        "diagnostic_present": True,
        "target_exists": False,
    }


def test_all_sources_are_revalidated_before_first_target_changes(
    tmp_path: Path,
) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    for name in ("alpha", "beta"):
        _agent(source_root, name, f"name: {name}\n")
        _agent(target_root, name, f"old: {name}\n")
    plans = build_link_plans(
        source_root=source_root,
        target_root=target_root,
        names=("alpha", "beta"),
        backup_root=tmp_path / "backup",
    )
    (source_root / "beta" / "spec.yaml").write_text("changed: true\n")

    # Act
    error = _capture_exception(lambda: apply_link_plans(plans))

    # Assert
    assert {
        "error": str(error),
        "targets": [
            (
                (target_root / name).is_symlink(),
                (target_root / name / "spec.yaml").read_text(),
            )
            for name in ("alpha", "beta")
        ],
    } == {
        "error": f"source changed after planning: {source_root / 'beta'}",
        "targets": [(False, "old: alpha\n"), (False, "old: beta\n")],
    }


def test_apply_rolls_back_every_prior_agent_on_mid_batch_failure(
    tmp_path: Path,
) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    for name in ("alpha", "beta"):
        _agent(source_root, name, f"name: {name}\n")
        _agent(target_root, name, f"old: {name}\n")
    plans = build_link_plans(
        source_root=source_root,
        target_root=target_root,
        names=("alpha", "beta"),
        backup_root=tmp_path / "backup",
    )
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("real filesystem fault\n")
    plans[1] = replace(
        plans[1],
        backup=str(blocked_parent / "beta"),
    )

    # Act
    error = _capture_exception(lambda: apply_link_plans(plans))

    # Assert
    assert {
        "error_type": type(error),
        "targets": [
            (
                (target_root / name).is_dir(),
                (target_root / name).is_symlink(),
                (target_root / name / "spec.yaml").read_text(),
            )
            for name in ("alpha", "beta")
        ],
    } == {
        "error_type": FileExistsError,
        "targets": [
            (True, False, "old: alpha\n"),
            (True, False, "old: beta\n"),
        ],
    }


def test_missing_later_source_prevents_any_apply(tmp_path: Path) -> None:
    # Arrange
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    _agent(source_root, "alpha", "name: alpha\n")
    old = _agent(target_root, "alpha", "old: alpha\n")

    # Act
    def build_invalid_plan() -> None:
        build_link_plans(
            source_root=source_root,
            target_root=target_root,
            names=("alpha", "missing"),
            backup_root=tmp_path / "backup",
        )

    error = _capture_exception(build_invalid_plan)

    # Assert
    assert {
        "error": str(error),
        "target_is_link": old.is_symlink(),
        "target_text": (old / "spec.yaml").read_text(),
    } == {
        "error": f"source agent must contain spec.yaml: {source_root / 'missing'}",
        "target_is_link": False,
        "target_text": "old: alpha\n",
    }


def test_cross_device_backup_is_refused_before_target_mutation(tmp_path: Path) -> None:
    # Arrange
    other_device = _other_device_or_skip(tmp_path)
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    _agent(source_root, "alpha", "name: alpha\n")
    old = _agent(target_root, "alpha", "old: alpha\n")
    plans = build_link_plans(
        source_root=source_root,
        target_root=target_root,
        names=("alpha",),
        backup_root=other_device / f"sac-link-specs-test-{os.getpid()}",
    )

    # Act
    error = _capture_exception(lambda: apply_link_plans(plans))

    # Assert
    assert {
        "error_type": type(error),
        "diagnostic": "same filesystem" in str(error),
        "target_is_link": old.is_symlink(),
        "target_text": (old / "spec.yaml").read_text(),
    } == {
        "error_type": click.ClickException,
        "diagnostic": True,
        "target_is_link": False,
        "target_text": "old: alpha\n",
    }
