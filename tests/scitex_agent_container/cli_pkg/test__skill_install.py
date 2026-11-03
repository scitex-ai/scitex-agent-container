"""SAC installation uses real Dev discovery, hashes and harness projections."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.skills_group import skills_group

PACKAGE = "scitex-agent-container"


@pytest.fixture
def owned_environment(tmp_path):
    saved = {name: os.environ.get(name) for name in ["HOME", "SCITEX_DIR"]}
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    os.environ.update(HOME=str(home), SCITEX_DIR=str(state))
    try:
        yield home, state
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _install(*flags):
    return CliRunner().invoke(skills_group, ["install", "--json", *flags])


@pytest.fixture
def installed(owned_environment):
    home, state = owned_environment
    result = _install()
    if result.exit_code:
        pytest.fail(result.output)
    return home, state, json.loads(result.stdout)


def test_install_resolves_real_scitex_config_root(installed):
    # Arrange
    _, state, payload = installed
    # Act
    destination = Path(payload["destination"])
    # Assert
    assert destination == state / "dev" / "skills"


def test_shared_install_has_real_projection_digest(installed):
    # Arrange
    _, _, payload = installed
    # Act
    manifest = json.loads(Path(payload["manifest"]).read_text())
    # Assert
    assert (
        payload["ok"],
        payload["projection_sha256"],
        {r["name"] for r in manifest["skills"]},
    ) == (True, manifest["projection_sha256"], {PACKAGE})


def test_default_links_hermes_to_validated_store(installed):
    # Arrange
    home, _, payload = installed
    # Act
    link = home / ".hermes" / "skills" / PACKAGE
    # Assert
    assert (link.is_symlink(), link.resolve()) == (
        True,
        (Path(payload["destination"]) / PACKAGE).resolve(),
    )


def test_no_link_forces_copy_instead_of_editable_symlink(owned_environment):
    # Arrange
    _, state = owned_environment
    # Act
    result = _install("--no-link", "--no-hermes-symlink")
    # Assert
    assert (result.exit_code, (state / "dev" / "skills" / PACKAGE).is_symlink()) == (
        0,
        False,
    )


def test_no_hermes_flag_does_not_expose_projection(owned_environment):
    # Arrange
    home, _ = owned_environment
    # Act
    result = _install("--no-hermes-symlink")
    # Assert
    assert (result.exit_code, (home / ".hermes").exists()) == (0, False)


def test_explicit_foreign_destination_is_preserved(owned_environment, tmp_path):
    # Arrange
    target = tmp_path / "foreign"
    note = target / PACKAGE / "my-work.txt"
    note.parent.mkdir(parents=True)
    note.write_text("foreign work")
    # Act
    result = _install("--dest", str(target))
    # Assert
    assert (
        result.exit_code,
        note.read_text(),
        (target / ".scitex-skills.json").exists(),
    ) == (1, "foreign work", False)


def test_foreign_hermes_directory_refuses_before_install(owned_environment):
    # Arrange
    home, state = owned_environment
    note = home / ".hermes" / "skills" / PACKAGE / "notes.txt"
    note.parent.mkdir(parents=True)
    note.write_text("do not replace")
    # Act
    result = _install()
    # Assert
    assert (result.exit_code, note.read_text(), state.exists()) == (
        1,
        "do not replace",
        False,
    )


def test_dry_run_leaves_skill_destinations_unmodified(owned_environment):
    # Arrange
    home, state = owned_environment
    # Act
    result = _install("--dry-run")
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["dry_run"],
        (state / "dev").exists(),
        (home / ".hermes").exists(),
    ) == (0, True, False, False)


def test_claude_projection_reuses_shared_store(owned_environment):
    # Arrange
    home, _ = owned_environment
    # Act
    result = _install("--claude-symlink")
    payload = json.loads(result.stdout)
    # Assert
    assert (result.exit_code, (home / ".claude" / "skills" / "scitex").resolve()) == (
        0,
        Path(payload["destination"]).resolve(),
    )


def test_missing_installer_capability_refuses_real_empty_interpreter(tmp_path):
    # Arrange
    import venv

    import click

    from scitex_agent_container.cli_pkg._skill_install import _require_shared_installer

    environment = tmp_path / "empty-interpreter"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Act
    try:
        _require_shared_installer(python=str(python))
        message = ""
    except click.ClickException as error:
        message = str(error)
    # Assert
    assert "transactional-projection-v1" in message


def test_repeat_install_preserves_projection_digest(installed):
    # Arrange
    _, _, before = installed
    # Act
    result = _install()
    after = json.loads(result.stdout)
    # Assert
    assert (result.exit_code, after["projection_sha256"]) == (
        0,
        before["projection_sha256"],
    )


@pytest.fixture
def copied_install(owned_environment):
    result = _install("--no-link", "--no-hermes-symlink")
    if result.exit_code:
        pytest.fail(result.output)
    return owned_environment


def test_dirty_managed_copy_refuses_replacement(copied_install):
    # Arrange
    _, state = copied_install
    store = state / "dev" / "skills"
    note = store / PACKAGE / "user-note.txt"
    note.write_text("keep dirty user work")
    manifest = (store / ".scitex-skills.json").read_bytes()
    # Act
    result = _install()
    # Assert
    assert (
        result.exit_code,
        note.read_text(),
        (store / ".scitex-skills.json").read_bytes(),
    ) == (1, "keep dirty user work", manifest)


def test_foreign_claude_directory_is_preserved_before_install(owned_environment):
    # Arrange
    home, state = owned_environment
    note = home / ".claude" / "skills" / "scitex" / "notes.txt"
    note.parent.mkdir(parents=True)
    note.write_text("foreign Claude skills")
    # Act
    result = _install("--claude-symlink")
    # Assert
    assert (result.exit_code, note.read_text(), (state / "dev").exists()) == (
        1,
        "foreign Claude skills",
        False,
    )
