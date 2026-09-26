from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg import _cards_source as source

EXPECTED_CARDS_COMMIT = "6e7fd467ba1c4bc77ed08e8a3ad45c17f8f46c5d"


def test_canonical_cards_source_pin_is_the_merged_commit():
    """Make an intentional test edit mandatory for every Cards pin rotation."""
    # Arrange
    expected = EXPECTED_CARDS_COMMIT

    # Act
    actual = source.CARDS_COMMIT

    # Assert
    assert actual == expected


def test_explicit_source_must_contain_pinned_commit(tmp_path):
    # Arrange
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    saved = os.environ.get(source.CARDS_SOURCE_ENV)
    os.environ[source.CARDS_SOURCE_ENV] = str(repository)

    # Act
    try:
        # Assert
        with pytest.raises(source.CardsSourceError, match="must name a git repository"):
            source.resolve_cards_repo()
    finally:
        if saved is None:
            os.environ.pop(source.CARDS_SOURCE_ENV, None)
        else:
            os.environ[source.CARDS_SOURCE_ENV] = saved


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
    (repository / "pyproject.toml").write_text("[project]\nname='scitex-cards'\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    saved_commit = source.CARDS_COMMIT
    saved_source = os.environ.get(source.CARDS_SOURCE_ENV)
    source.CARDS_COMMIT = commit
    os.environ[source.CARDS_SOURCE_ENV] = str(repository)
    build_context = tmp_path / "build-context"
    build_context.mkdir()

    # Act
    try:
        staged = source.stage_cards_source(build_context)
    finally:
        source.CARDS_COMMIT = saved_commit
        if saved_source is None:
            os.environ.pop(source.CARDS_SOURCE_ENV, None)
        else:
            os.environ[source.CARDS_SOURCE_ENV] = saved_source

    # Assert
    assert (
        (staged / "pyproject.toml").is_file()
        and (staged / "SAC_UPSTREAM_COMMIT").read_text() == f"{commit}\n"
        and not (staged / ".git").exists()
    )


def test_base_recipe_installs_and_labels_exact_staged_commit():
    # Arrange
    recipe = Path(source.__file__).parent.parent / "containers" / "apptainer-base.def"

    # Act
    text = recipe.read_text(encoding="utf-8")

    # Assert
    assert all(
        expected in text
        for expected in (
            "scitex-cards-src /opt/scitex-cards-src",
            '"/opt/scitex-cards-src[mcp,postgres]"',
            "--reinstall-package scitex-cards",
            f'cat /opt/scitex-cards-src/SAC_UPSTREAM_COMMIT)" = "{source.CARDS_COMMIT}"',
            f"org.scitex.cards.commit {source.CARDS_COMMIT}",
        )
    )


def test_scitex_recipe_reinstalls_exact_source_after_dependency_resolution():
    # Arrange
    recipe = Path(source.__file__).parent.parent / "containers" / "apptainer-scitex.def"

    # Act
    text = recipe.read_text(encoding="utf-8")

    # Assert
    assert all(
        expected in text
        for expected in (
            "scitex-cards-src /opt/scitex-cards-src",
            "--reinstall-package scitex-cards",
            '"/opt/scitex-cards-src[mcp,postgres]"',
            "/opt/scitex-cards-src/scitex-cards-src",
            f'cat /opt/scitex-cards-src/SAC_UPSTREAM_COMMIT)" = "{source.CARDS_COMMIT}"',
            f"org.scitex.cards.commit {source.CARDS_COMMIT}",
        )
    )
