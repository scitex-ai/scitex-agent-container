from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import _agents_link_specs as subject
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


def test_current_link_is_an_idempotent_plan(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    target_root.mkdir()
    (target_root / "alpha").symlink_to(source, target_is_directory=True)

    plans = build_link_plans(
        source_root=source_root,
        target_root=target_root,
        names=("alpha",),
        backup_root=tmp_path / "backup",
    )

    assert plans[0].state == "current"
    assert plans[0].backup is None
    apply_link_plans(plans)
    assert (target_root / "alpha").resolve() == source.resolve()


def test_cli_is_dry_run_by_default_and_reports_git_identity(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    old = _agent(target_root, "alpha", "old: true\n")
    _git_repo(source_root)

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

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert payload["ok"] is False
    assert payload["git"]["head"]
    assert payload["git"]["source_dirty"] is False
    assert payload["agents"][0]["state"] == "archive_and_link"
    assert not old.is_symlink()
    assert (old / "spec.yaml").read_text() == "old: true\n"


def test_apply_archives_real_tree_then_installs_absolute_link(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    backup_root = tmp_path / "backup"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    _agent(target_root, "alpha", "old: true\n")
    _git_repo(source_root)

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

    assert result.exit_code == 0, result.output
    live = target_root / "alpha"
    assert live.is_symlink()
    assert os.readlink(live) == str(source.resolve())
    assert (backup_root / "alpha" / "spec.yaml").read_text() == "old: true\n"


def test_apply_refuses_uncommitted_selected_source(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    source = _agent(source_root, "alpha", "apiVersion: sac/v3\n")
    _git_repo(source_root)
    (source / "spec.yaml").write_text("changed: true\n")

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

    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert not (target_root / "alpha").exists()


def test_all_sources_are_revalidated_before_first_target_changes(
    tmp_path: Path,
) -> None:
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

    with pytest.raises(Exception, match="source changed after planning"):
        apply_link_plans(plans)

    for name in ("alpha", "beta"):
        assert not (target_root / name).is_symlink()
        assert (target_root / name / "spec.yaml").read_text() == f"old: {name}\n"


def test_apply_rolls_back_every_prior_agent_on_mid_batch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    real_replace = subject.os.replace
    calls = 0

    def fail_fourth_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected link publication failure")
        real_replace(source, target)

    monkeypatch.setattr(subject.os, "replace", fail_fourth_replace)

    with pytest.raises(OSError, match="injected"):
        apply_link_plans(plans)

    for name in ("alpha", "beta"):
        live = target_root / name
        assert live.is_dir() and not live.is_symlink()
        assert (live / "spec.yaml").read_text() == f"old: {name}\n"


def test_missing_later_source_prevents_any_apply(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "live"
    _agent(source_root, "alpha", "name: alpha\n")
    old = _agent(target_root, "alpha", "old: alpha\n")

    with pytest.raises(Exception, match="source agent must contain spec.yaml"):
        build_link_plans(
            source_root=source_root,
            target_root=target_root,
            names=("alpha", "missing"),
            backup_root=tmp_path / "backup",
        )

    assert not old.is_symlink()
    assert (old / "spec.yaml").read_text() == "old: alpha\n"
