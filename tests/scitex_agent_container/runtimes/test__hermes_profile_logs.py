"""Generated-log controls use only synthetic profiles; no account/store calls."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _hermes_profile_logs as log_profile


def test_fresh_profile_has_private_appendable_logs(tmp_path):
    # Arrange / Act
    assert log_profile.ensure_hermes_log_files(tmp_path) == ()

    # Assert
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    for name in ("agent.log", "gui.log"):
        path = tmp_path / "logs" / name
        assert stat.S_ISREG(path.lstat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with path.open("a", encoding="utf-8") as stream:
            stream.write("synthetic startup\n")
        assert path.read_text() == "synthetic startup\n"


@pytest.mark.parametrize("target_exists", [False, True])
def test_archives_link_objects_without_following_targets(tmp_path, target_exists):
    # Arrange
    profile = tmp_path / "profile"
    logs = profile / "logs"
    logs.mkdir(parents=True)
    target = tmp_path / "foreign-home" / "log"
    if target_exists:
        target.parent.mkdir()
        target.write_text("foreign history\n")
    links = [logs / name for name in ("agent.log", "gui.log")]
    for link in links:
        link.symlink_to(target)
    original_inodes = {link.name: link.lstat().st_ino for link in links}

    # Act
    archives = log_profile.ensure_hermes_log_files(profile)
    assert log_profile.ensure_hermes_log_files(profile) == ()

    # Assert
    assert len(archives) == 2
    for name, archive in zip(("agent.log", "gui.log"), archives):
        assert archive.is_symlink()
        assert os.readlink(archive) == str(target)
        assert archive.lstat().st_ino == original_inodes[name]
        assert stat.S_ISREG((logs / name).lstat().st_mode)
        with (logs / name).open("a") as stream:
            stream.write("local startup\n")
    assert target.exists() == target_exists
    if target_exists:
        assert target.read_text() == "foreign history\n"


def test_existing_logs_rotations_and_archives_are_preserved(tmp_path):
    # Arrange
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    for name in ("agent.log", "gui.log", "agent.log.1", "history.log"):
        (logs / name).write_text(f"{name} history\n")
    archive = logs / "agent.log.sac-link-preserved"
    archive.symlink_to("/synthetic/previous-home/log")
    before = {path.name: path.lstat() for path in logs.iterdir()}

    # Act
    assert log_profile.ensure_hermes_log_files(tmp_path) == ()
    assert log_profile.ensure_hermes_log_files(tmp_path) == ()

    # Assert
    assert {path.name for path in logs.iterdir()} == set(before)
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    for name, metadata in before.items():
        assert (logs / name).lstat().st_ino == metadata.st_ino
        assert (logs / name).lstat().st_mtime_ns == metadata.st_mtime_ns
        if name != archive.name:
            assert (logs / name).read_text() == f"{name} history\n"
    assert os.readlink(archive) == "/synthetic/previous-home/log"


def test_archive_name_collision_preserves_existing_archive(tmp_path, monkeypatch):
    # Arrange
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").symlink_to("/synthetic/imported/log")
    previous = logs / "agent.log.sac-link-preserved"
    previous.symlink_to("/synthetic/previous/log")
    names = iter(("preserved", "new"))
    monkeypatch.setattr(log_profile, "uuid4", lambda: SimpleNamespace(hex=next(names)))

    # Act
    archives = log_profile.ensure_hermes_log_files(tmp_path)

    # Assert
    assert archives == (logs / "agent.log.sac-link-new",)
    assert os.readlink(previous) == "/synthetic/previous/log"
    assert os.readlink(archives[0]) == "/synthetic/imported/log"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_unexpected_generated_entry_fails_before_any_log_changes(tmp_path, kind):
    # Arrange
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").symlink_to("/synthetic/missing/log")
    unexpected = logs / "gui.log"
    if kind == "directory":
        unexpected.mkdir()
    else:
        os.mkfifo(unexpected)
    before = {path.name: path.lstat().st_ino for path in logs.iterdir()}

    # Act
    with pytest.raises(ValueError, match="not a file or link"):
        log_profile.ensure_hermes_log_files(tmp_path)

    # Assert
    assert {path.name: path.lstat().st_ino for path in logs.iterdir()} == before
    assert (logs / "agent.log").is_symlink()


def test_symlinked_log_directory_is_rejected_without_touching_target(tmp_path):
    # Arrange
    target = tmp_path / "foreign-logs"
    target.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "logs").symlink_to(target, target_is_directory=True)

    # Act
    with pytest.raises(OSError):
        log_profile.ensure_hermes_log_files(profile)

    # Assert
    assert list(target.iterdir()) == []
    assert (profile / "logs").is_symlink()


@pytest.mark.parametrize("launch_mode", ["sdk", "tui"])
def test_both_materializers_repair_each_home_backing(
    tmp_path, monkeypatch, launch_mode
):
    # Arrange: replace all provider, credential, store, and deployment work.
    from scitex_agent_container.runtimes import (
        _hermes_cct,
        _hermes_profile,
        _pg_identity_credentials,
    )

    state = tmp_path / "state"
    upper = tmp_path / "synthetic-upper-home"
    targets = [state / "home", upper]
    for home in targets:
        logs = home / ".hermes" / "logs"
        logs.mkdir(parents=True)
        (logs / "agent.log").symlink_to("/synthetic/old-home/log")
        (logs / "gui.log").symlink_to("/synthetic/old-home/gui-log")

    def noop(*args, **kwargs):
        pass

    for name in ("deploy_to_home", "setup_mcp_config"):
        monkeypatch.setattr(_hermes_profile, name, noop)
    for name in ("deploy_to_home_overlay", "resolve_overlay_upper_home"):
        monkeypatch.setattr(_hermes_profile, name, lambda *args: upper)
    monkeypatch.setattr(
        _hermes_profile, "ensure_api_key", lambda *args: "synthetic-key"
    )
    monkeypatch.setattr(
        _hermes_profile, "resolve_provider_api_key", lambda *args: "synthetic"
    )
    monkeypatch.setattr(
        _hermes_profile, "_verified_instruction_text", lambda *args: "synthetic"
    )
    monkeypatch.setattr(
        _hermes_profile,
        "_launch_plan",
        lambda *args, **kwargs: SimpleNamespace(
            endpoint=SimpleNamespace(auth_env="SYNTHETIC_KEY")
        ),
    )
    monkeypatch.setattr(
        _hermes_profile, "compile_hermes_config", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        _hermes_profile, "_mcp_servers", lambda *args, **kwargs: ({}, [])
    )
    monkeypatch.setattr(_hermes_profile, "_sac_profile_env", lambda *args: {})
    monkeypatch.setattr(_hermes_profile, "_cct_profile_env", lambda *args: {})
    monkeypatch.setattr(_hermes_cct, "wire_hermes_cct_rail", lambda *args, **kwargs: {})
    monkeypatch.setattr(_pg_identity_credentials, "materialize_project_pgpass", noop)
    config = SimpleNamespace(
        name="synthetic",
        workdir="/synthetic",
        hermes_run_budget_seconds=60,
        hermes_compression=None,
        hermes_background_review=None,
        claude=SimpleNamespace(channels=None),
    )

    # Act
    if launch_mode == "sdk":
        _hermes_profile.materialize_hermes_profile(
            config, state_dir=state, api_port=19000
        )
    else:
        _hermes_profile.materialize_hermes_tui_profile(config, state_dir=state)

    # Assert: the actual materializers must call the real repair for both homes.
    for home in targets:
        logs = home / ".hermes" / "logs"
        for name in ("agent.log", "gui.log"):
            assert stat.S_ISREG((logs / name).lstat().st_mode)
            assert len(list(logs.glob(f"{name}.sac-link-*"))) == 1
