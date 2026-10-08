"""Generated-log controls use synthetic profiles and isolated boundary fakes."""

from __future__ import annotations

import builtins
import os
import stat
from types import FunctionType, SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _hermes_profile_logs as log_profile


def _bind(function, namespace):
    bound = FunctionType(
        function.__code__,
        namespace,
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    bound.__kwdefaults__ = function.__kwdefaults__
    return bound


def _snapshot(logs):
    entries = {}
    for path in logs.iterdir():
        info = path.lstat()
        contents = (
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else None
        )
        entries[path.name] = (info.st_ino, info.st_mode, info.st_mtime_ns, contents)
    return stat.S_IMODE(logs.stat().st_mode), entries


@pytest.fixture(params=[False, True])
def imported_profile(tmp_path, request):
    profile = tmp_path / "profile"
    logs = profile / "logs"
    logs.mkdir(parents=True)
    target = tmp_path / "foreign-home" / "log"
    if request.param:
        target.parent.mkdir()
        target.write_text("foreign history\n")
    links = [logs / name for name in ("agent.log", "gui.log")]
    for link in links:
        link.symlink_to(target)
    return (
        profile,
        target,
        {link.name: (True, link.lstat().st_ino, str(target)) for link in links},
    )


@pytest.mark.parametrize("name", ["agent.log", "gui.log"])
@pytest.mark.parametrize(
    "fact, expected",
    [("regular", True), ("mode", 0o600), ("append", "synthetic startup\n")],
)
def test_fresh_generated_logs_are_private_and_appendable(
    tmp_path, name, fact, expected
):
    # Arrange
    path = tmp_path / "logs" / name

    # Act
    log_profile.ensure_hermes_log_files(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("synthetic startup\n")
    observed = {
        "regular": stat.S_ISREG(path.lstat().st_mode),
        "mode": stat.S_IMODE(path.stat().st_mode),
        "append": path.read_text(),
    }

    # Assert
    assert observed[fact] == expected


def test_fresh_generated_log_directory_is_private(tmp_path):
    # Arrange
    logs = tmp_path / "logs"

    # Act
    log_profile.ensure_hermes_log_files(tmp_path)

    # Assert
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


def test_imported_link_objects_are_archived_without_dereferencing(imported_profile):
    # Arrange
    profile, _target, expected = imported_profile

    # Act
    archives = log_profile.ensure_hermes_log_files(profile)
    observed = {
        path.name.split(".sac-link-")[0]: (
            path.is_symlink(),
            path.lstat().st_ino,
            os.readlink(path),
        )
        for path in archives
    }

    # Assert
    assert observed == expected


@pytest.mark.parametrize("name", ["agent.log", "gui.log"])
def test_imported_links_become_regular_generated_files(imported_profile, name):
    # Arrange
    profile, _target, _links = imported_profile

    # Act
    log_profile.ensure_hermes_log_files(profile)

    # Assert
    assert stat.S_ISREG((profile / "logs" / name).lstat().st_mode)


def test_generated_append_never_changes_foreign_target(imported_profile):
    # Arrange
    profile, target, _links = imported_profile
    expected = target.read_bytes() if target.exists() else None

    # Act
    log_profile.ensure_hermes_log_files(profile)
    for name in ("agent.log", "gui.log"):
        with (profile / "logs" / name).open("a") as stream:
            stream.write("local startup\n")
    observed = target.read_bytes() if target.exists() else None

    # Assert
    assert observed == expected


def test_repaired_profile_is_idempotent_without_additional_archives(imported_profile):
    # Arrange
    profile, _target, _links = imported_profile
    log_profile.ensure_hermes_log_files(profile)

    # Act
    repeated = log_profile.ensure_hermes_log_files(profile)

    # Assert
    assert repeated == ()


def test_existing_regular_rotated_history_and_archive_logs_are_preserved(tmp_path):
    # Arrange
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    for name in ("agent.log", "gui.log", "agent.log.1", "history.log"):
        (logs / name).write_text(f"{name} history\n")
    (logs / "agent.log.sac-link-preserved").symlink_to("/synthetic/previous/log")
    expected = _snapshot(logs)

    # Act
    log_profile.ensure_hermes_log_files(tmp_path)
    log_profile.ensure_hermes_log_files(tmp_path)

    # Assert
    assert _snapshot(logs) == expected


def test_archive_name_collision_preserves_previous_and_imported_links(tmp_path):
    # Arrange: bind real helper code to a deterministic archive-name source.
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").symlink_to("/synthetic/imported/log")
    (logs / "agent.log.sac-link-preserved").symlink_to("/synthetic/previous/log")
    names = iter(("preserved", "new"))
    namespace = {**vars(log_profile), "uuid4": lambda: SimpleNamespace(hex=next(names))}
    namespace["_archive_link"] = _bind(log_profile._archive_link, namespace)
    provision = _bind(log_profile.ensure_hermes_log_files, namespace)

    # Act
    provision(tmp_path)
    observed = {
        name: os.readlink(logs / name)
        for name in ("agent.log.sac-link-preserved", "agent.log.sac-link-new")
    }

    # Assert
    assert observed == {
        "agent.log.sac-link-preserved": "/synthetic/previous/log",
        "agent.log.sac-link-new": "/synthetic/imported/log",
    }


@pytest.fixture(params=["directory", "fifo"])
def unexpected_profile(tmp_path, request):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").symlink_to("/synthetic/missing/log")
    unexpected = logs / "gui.log"
    if request.param == "directory":
        unexpected.mkdir()
    else:
        os.mkfifo(unexpected)
    return tmp_path


def test_unexpected_generated_entry_is_refused_before_provisioning(unexpected_profile):
    # Arrange
    profile = unexpected_profile

    # Act
    def call():
        return log_profile.ensure_hermes_log_files(profile)

    # Assert
    with pytest.raises(ValueError, match="not a file or link"):
        call()


def _attempt_refused_provision(profile, error_type):
    try:
        log_profile.ensure_hermes_log_files(profile)
    except error_type:
        return


def test_unexpected_entry_preserves_all_existing_log_objects(unexpected_profile):
    # Arrange
    logs = unexpected_profile / "logs"
    expected = _snapshot(logs)

    # Act
    _attempt_refused_provision(unexpected_profile, ValueError)

    # Assert
    assert _snapshot(logs) == expected


@pytest.fixture
def symlink_log_directory(tmp_path):
    target = tmp_path / "foreign-logs"
    target.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "logs").symlink_to(target, target_is_directory=True)
    return profile, target


def test_symlinked_log_directory_is_refused_without_dereferencing(
    symlink_log_directory,
):
    # Arrange
    profile, _target = symlink_log_directory

    # Act
    def call():
        return log_profile.ensure_hermes_log_files(profile)

    # Assert
    with pytest.raises(OSError):
        call()


def test_symlinked_log_directory_preserves_foreign_target_contents(
    symlink_log_directory,
):
    # Arrange
    profile, target = symlink_log_directory
    expected = _snapshot(target)

    # Act
    _attempt_refused_provision(profile, OSError)

    # Assert
    assert _snapshot(target) == expected


@pytest.fixture(params=["sdk", "tui"])
def materialized_home_backings(tmp_path, request):
    from scitex_agent_container.runtimes import _hermes_profile as profile

    state = tmp_path / "state"
    upper = tmp_path / "synthetic-upper-home"
    targets = [state / "home", upper]
    for home in targets:
        logs = home / ".hermes" / "logs"
        logs.mkdir(parents=True)
        for name in ("agent.log", "gui.log"):
            (logs / name).symlink_to("/synthetic/old-home/log")
    fake_modules = {
        "_hermes_cct": SimpleNamespace(wire_hermes_cct_rail=lambda *args, **kwargs: {}),
        "_pg_identity_credentials": SimpleNamespace(
            materialize_project_pgpass=lambda *args, **kwargs: None
        ),
    }

    def import_boundary(name, *args, **kwargs):
        return (
            fake_modules[name]
            if name in fake_modules
            else builtins.__import__(name, *args, **kwargs)
        )

    namespace = {
        **vars(profile),
        "__builtins__": {**vars(builtins), "__import__": import_boundary},
        "deploy_to_home": lambda *args: None,
        "setup_mcp_config": lambda *args: None,
        "deploy_to_home_overlay": lambda *args: upper,
        "resolve_overlay_upper_home": lambda *args: upper,
        "ensure_api_key": lambda *args: "synthetic-key",
        "resolve_provider_api_key": lambda *args: "synthetic",
        "_verified_instruction_text": lambda *args: "synthetic",
        "_launch_plan": lambda *args, **kwargs: SimpleNamespace(
            endpoint=SimpleNamespace(auth_env="SYNTHETIC_KEY")
        ),
        "compile_hermes_config": lambda *args, **kwargs: {},
        # The verified-route seam probes declared pools before deploying
        # files; this log-repair test pins the seam's outputs instead.
        "_verified_route": lambda *args, **kwargs: (
            SimpleNamespace(endpoint=SimpleNamespace(auth_env="SYNTHETIC_KEY")),
            {},
            {},
            {},
        ),
        "_profile_primary_key": lambda *args: "synthetic",
        "_mcp_servers": lambda *args, **kwargs: ({}, []),
        "_sac_profile_env": lambda *args: {},
        "_cct_profile_env": lambda *args: {},
    }
    config = SimpleNamespace(
        name="synthetic",
        workdir="/synthetic",
        engine_key="synthetic",
        runtime="tui",
        hermes_failover=SimpleNamespace(accounts={}, engines=[]),
        hermes_run_budget_seconds=60,
        hermes_compression=None,
        hermes_background_review=None,
        claude=SimpleNamespace(channels=None),
    )
    sdk = _bind(profile.materialize_hermes_profile, namespace)
    tui = _bind(profile.materialize_hermes_tui_profile, namespace)
    calls = {
        "sdk": lambda: sdk(config, state_dir=state, api_port=19_000),
        "tui": lambda: tui(config, state_dir=state),
    }
    return targets, calls[request.param]


def test_materializers_repair_each_declared_home_backing(materialized_home_backings):
    # Arrange
    targets, materialize = materialized_home_backings

    # Act
    materialize()
    observed = {
        (index, name): (
            stat.S_ISREG((home / ".hermes" / "logs" / name).lstat().st_mode),
            len(list((home / ".hermes" / "logs").glob(f"{name}.sac-link-*"))),
        )
        for index, home in enumerate(targets)
        for name in ("agent.log", "gui.log")
    }

    # Assert
    assert observed == {
        (index, name): (True, 1)
        for index in range(2)
        for name in ("agent.log", "gui.log")
    }
