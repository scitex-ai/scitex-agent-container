from __future__ import annotations

import os
import subprocess

import pytest

from scitex_agent_container.cli_pkg import _hermes_source as source


def test_pin_names_the_validated_sac_hermes_source() -> None:
    # Arrange
    expected = (
        "https://github.com/NousResearch/hermes-agent.git",
        "eb442593b66a255f7f7789d4bf2e862d861cec61",
    )

    # Act
    pin = (source.HERMES_REPOSITORY, source.HERMES_COMMIT)

    # Assert — never replace this immutable pair with a mutable PR ref.
    assert pin == expected


def test_explicit_source_must_contain_pinned_commit(tmp_path):
    # Arrange
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    saved = os.environ.get(source.HERMES_SOURCE_ENV)
    os.environ[source.HERMES_SOURCE_ENV] = str(repository)

    # Act
    # Assert
    try:
        with pytest.raises(
            source.HermesSourceError, match="must name a git repository"
        ):
            source.resolve_hermes_repo()
    finally:
        if saved is None:
            os.environ.pop(source.HERMES_SOURCE_ENV, None)
        else:
            os.environ[source.HERMES_SOURCE_ENV] = saved


def test_stage_exports_pinned_tree_without_git_metadata(tmp_path):
    # Arrange
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "pyproject.toml").write_text("[project]\nname='hermes'\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    saved_commit = source.HERMES_COMMIT
    saved_repository = source.HERMES_REPOSITORY
    saved_source = os.environ.get(source.HERMES_SOURCE_ENV)
    source.HERMES_COMMIT = commit
    source.HERMES_REPOSITORY = "https://example.invalid/hermes-agent.git"
    os.environ[source.HERMES_SOURCE_ENV] = str(repository)

    build_context = tmp_path / "build-context"
    build_context.mkdir()

    # Act
    try:
        staged = source.stage_hermes_source(build_context)
    finally:
        source.HERMES_COMMIT = saved_commit
        source.HERMES_REPOSITORY = saved_repository
        if saved_source is None:
            os.environ.pop(source.HERMES_SOURCE_ENV, None)
        else:
            os.environ[source.HERMES_SOURCE_ENV] = saved_source

    # Assert
    assert (
        (staged / "pyproject.toml").is_file()
        and (staged / "SAC_UPSTREAM_COMMIT").read_text() == f"{commit}\n"
        and (staged / "SAC_UPSTREAM_REPOSITORY").read_text()
        == "https://example.invalid/hermes-agent.git\n"
        and not (staged / ".git").exists()
    )
