"""Tests for the build stamp baked into a distribution.

Real git repos and real generated files in ``tmp_path``. The
highest-stakes case here is ``inherits`` — ``python -m build`` builds the
wheel FROM THE UNPACKED SDIST, a temp dir with no ``.git``. If the stamp
did not survive that hop, every wheel ever published would carry an
unknown commit, and the wheel is the only artifact anyone installs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._provenance._stamp import (
    COMMIT_ENV_VAR,
    compute_stamp,
    read_existing_stamp,
    render_module,
    stamp_path,
)


@pytest.fixture
def commit_env():
    """Set the real build-commit env var; restore it on teardown."""
    saved = os.environ.get(COMMIT_ENV_VAR)

    def _set(value: str) -> None:
        os.environ[COMMIT_ENV_VAR] = value

    try:
        yield _set
    finally:
        if saved is None:
            os.environ.pop(COMMIT_ENV_VAR, None)
        else:
            os.environ[COMMIT_ENV_VAR] = saved


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real src-layout git checkout with one commit."""
    root = tmp_path / "proj"
    package = root / "src" / "scitex_agent_container"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def package(project: Path) -> Path:
    return project / "src" / "scitex_agent_container"


class TestComputeStamp:
    def test_reads_the_commit_from_the_checkout(self, project: Path, package: Path):
        # Arrange
        expected = _git(project, "rev-parse", "HEAD")

        # Act
        stamp = compute_stamp(project, package, version="1.2.3")

        # Assert
        assert stamp["commit"] == expected

    def test_records_git_as_the_commit_source(self, project: Path, package: Path):
        # Arrange
        expected = "git"

        # Act
        stamp = compute_stamp(project, package, version="1.2.3")

        # Assert
        assert stamp["commit_source"] == expected

    def test_env_override_beats_the_checkout(
        self, project: Path, package: Path, commit_env
    ):
        # Arrange — CI injects the sha it actually checked out.
        commit_env("f" * 40)

        # Act
        stamp = compute_stamp(project, package, version="1.2.3")

        # Assert
        assert stamp["commit"] == "f" * 40

    def test_inherits_the_commit_when_git_is_absent(self, tmp_path: Path):
        # Arrange — reproduce the sdist->wheel hop exactly: an unpacked
        # sdist, no .git anywhere, carrying the stamp the sdist build wrote
        # AT THE REAL PATH the build hook writes it to. Writing it anywhere
        # else is what let this test pass while the actual chain baked
        # commit=unknown into every wheel.
        root = tmp_path / "unpacked-sdist"
        package = root / "src" / "scitex_agent_container"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 1\n")
        stamp_file = stamp_path(package)
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text("STAMP = {'commit': 'abc123', 'version': '1.2.3'}\n")

        # Act
        stamp = compute_stamp(root, package, version="1.2.3")

        # Assert
        assert stamp["commit"] == "abc123"

    def test_marks_an_inherited_commit_as_inherited(self, tmp_path: Path):
        # Arrange
        root = tmp_path / "unpacked-sdist"
        package = root / "src" / "scitex_agent_container"
        package.mkdir(parents=True)
        stamp_file = stamp_path(package)
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text("STAMP = {'commit': 'abc123'}\n")

        # Act
        stamp = compute_stamp(root, package, version="1.2.3")

        # Assert
        assert stamp["commit_source"] == "inherited"

    def test_still_records_a_code_hash_without_any_commit(self, tmp_path: Path):
        # Arrange — no git, no prior stamp. The scheme must NOT degrade to
        # useless: the content hash still moves when the code moves.
        root = tmp_path / "bare"
        package = root / "src" / "scitex_agent_container"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 1\n")

        # Act
        stamp = compute_stamp(root, package, version="1.2.3")

        # Assert
        assert stamp["code_hash"]

    def test_reports_an_unknown_commit_source_with_nothing_to_read(
        self, tmp_path: Path
    ):
        # Arrange
        root = tmp_path / "bare"
        package = root / "src" / "scitex_agent_container"
        package.mkdir(parents=True)

        # Act
        stamp = compute_stamp(root, package, version="1.2.3")

        # Assert
        assert stamp["commit_source"] == "unknown"


class TestRenderModule:
    def test_rendered_stamp_reads_back_identically(self, tmp_path: Path):
        # Arrange — the generated module must be parseable by the reader
        # that the next build stage uses to inherit from it.
        package = tmp_path / "pkg"
        package.mkdir()
        stamp = {
            "version": "1.2.3",
            "commit": "a" * 40,
            "commit_source": "git",
            "code_hash": "deadbeef",
            "built_at": "2026-07-13T00:00:00Z",
        }

        # Act
        stamp_file = stamp_path(package)
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text(render_module(stamp))
        found = read_existing_stamp(package)

        # Assert
        assert found == stamp


class TestStampPath:
    def test_the_build_hook_writes_where_the_reader_looks(self, tmp_path: Path):
        # Arrange — the regression that shipped commit=unknown into the
        # wheel: the hook wrote <pkg>/_provenance/_build_info.py while the
        # reader looked in <pkg>/_build_info.py, so the sdist->wheel
        # inherit silently found nothing. Pin the two together.
        package = tmp_path / "src" / "scitex_agent_container"
        package.mkdir(parents=True)
        written = stamp_path(package)
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(render_module({"commit": "abc123"}))

        # Act
        found = read_existing_stamp(package)

        # Assert
        assert found["commit"] == "abc123"


class TestReadExistingStamp:
    def test_absent_stamp_reads_as_none(self, tmp_path: Path):
        # Arrange
        package = tmp_path / "pkg"
        package.mkdir()

        # Act
        found = read_existing_stamp(package)

        # Assert
        assert found is None

    def test_corrupt_stamp_reads_as_none(self, tmp_path: Path):
        # Arrange — a half-written generated file must not break the build.
        package = tmp_path / "pkg"
        package.mkdir()
        stamp_file = stamp_path(package)
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text("STAMP = {'commit': \n")

        # Act
        found = read_existing_stamp(package)

        # Assert
        assert found is None

# EOF
