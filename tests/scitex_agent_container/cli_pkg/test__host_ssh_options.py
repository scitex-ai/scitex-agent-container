"""SSH option discovery preserves argv boundaries without opening connections."""

import json
import shlex

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main


@pytest.fixture(params=["space-path", "disabled", "unusable-path"])
def option_environment(tmp_path, env_save_restore, request):
    """Only an explicit owned pin; preserve existing opt-out/fallback policy."""
    directory = tmp_path / "control sockets"
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(directory))
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "auto")
    expected = [
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ControlPath={directory / '%C'}",
    ]
    if request.param == "disabled":
        env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
        expected = []
    if request.param == "unusable-path":
        directory.write_bytes(b"preserve-owned-file")
        expected = []
    return directory, expected, request.param


@pytest.mark.parametrize(
    "args,legacy",
    [
        (["host", "show-ssh-options", "--json"], False),
        (["--json", "host", "show-ssh-options"], False),
        (["host", "ssh-opts", "--json"], True),
        (["--json", "host", "ssh-opts"], True),
    ],
)
def test_ssh_json_preserves_option_words_and_backend_policy(
    option_environment, args, legacy
):
    # Arrange
    directory, expected, _mode = option_environment
    # Act
    result = CliRunner().invoke(main, args)
    data = json.loads(result.stdout)
    # Assert
    assert (result.exit_code, data, "show-ssh-options" in result.stderr) == (
        0,
        {"options": expected},
        legacy,
    )


@pytest.mark.parametrize("name", ["show-ssh-options", "ssh-opts"])
def test_ssh_human_string_roundtrips_without_resplitting_paths(
    option_environment, name
):
    # Arrange
    _directory, expected, _mode = option_environment
    # Act
    result = CliRunner().invoke(main, ["host", name])
    options = shlex.split(result.stdout)
    # Assert
    assert (result.exit_code, options) == (0, expected)


def test_ssh_optout_does_not_create_control_directory(tmp_path, env_save_restore):
    # Arrange
    directory = tmp_path / "not-created"
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(directory))
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    # Act
    result = CliRunner().invoke(main, ["host", "show-ssh-options", "--json"])
    # Assert
    assert (result.exit_code, json.loads(result.stdout), directory.exists()) == (
        0,
        {"options": []},
        False,
    )


def test_ssh_unusable_pin_preserves_existing_file(tmp_path, env_save_restore):
    # Arrange
    directory = tmp_path / "occupied"
    directory.write_bytes(b"preserve-owned-file")
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(directory))
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "auto")
    # Act
    result = CliRunner().invoke(main, ["host", "show-ssh-options", "--json"])
    # Assert
    assert (result.exit_code, json.loads(result.stdout), directory.read_bytes()) == (
        0,
        {"options": []},
        b"preserve-owned-file",
    )
