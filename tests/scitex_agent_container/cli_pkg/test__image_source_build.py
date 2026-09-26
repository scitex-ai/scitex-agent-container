"""Tests for ``cli_pkg/_image_source_build.py``.

Covers the staging helper itself plus two integration contracts the
helper depends on (consolidated here, not in a separate test file, to
satisfy PS-204 §2 orphan-test-file: every test file must mirror a
single src file):

  - ``stage_build_context`` / ``build_layer_from_source`` /
    ``resolve_bootstrap_sif`` — unit coverage of the helper. The build
    delegates to scitex-container's atomic ``build`` via the module-level
    ``_container_build`` seam (swapped for a real recording fake).
  - Shipped .def contract — every apptainer-*.def in the package's
    ``containers/`` dir must declare the bundled-source ``%files``
    entry, install sac from ``/opt/scitex-agent-container-src``, and
    NOT reference any ``git+...`` install. Locks the .def-side of the
    helper's invariant; without this, a future .def edit could silently
    drop back to ``git+https://...@main`` and the SIF version would
    drift from the source tree that shipped it.
  - Wheel-ships contract — the built wheel must contain pyproject.toml
    + README.md under ``scitex_agent_container/_bundled/`` (via the
    ``[tool.hatch.build.targets.wheel.force-include]`` directive). The
    helper's ``locate_bundled_pyproject``/``locate_bundled_readme`` fall
    through to the editable repo root only as a fallback; for
    wheel-installed sac, ``_bundled/`` is the source of truth.

No MagicMock anywhere: tests construct hand-rolled real callables and
swap module-level references the same way ``test_image_group`` does.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import tomllib

import scitex_agent_container
from scitex_agent_container._provenance._git import head_sha
from scitex_agent_container._provenance._hash import code_hash
from scitex_agent_container._provenance._stamp import (
    compute_stamp,
    read_existing_stamp,
    stamp_path,
)
from scitex_agent_container.cli_pkg import _image_source_build as isb
from scitex_agent_container.cli_pkg.image_group import _LAYERS, _RECIPES_DIR

_LOADED_PACKAGE_ROOT = Path(scitex_agent_container.__file__).resolve().parent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pkg_root(tmp_path: Path) -> Path:
    """A real on-disk fake of the installed package tree (no MagicMock).

    Mirrors a wheel-install layout: the package dir contains an
    ``_bundled/pyproject.toml`` (which the real wheel ships via
    ``[tool.hatch.build.targets.wheel.force-include]``), so
    :func:`locate_bundled_pyproject` finds it on the wheel-install
    branch. Also seeds a ``__pycache__`` to verify the ignore patterns.
    """
    root = tmp_path / "scitex_agent_container"
    root.mkdir()
    (root / "__init__.py").write_text("__version__ = '0.0.0-test'\n")
    sub = root / "cli_pkg"
    sub.mkdir()
    (sub / "__init__.py").write_text("\n")
    (sub / "image_group.py").write_text("# fake\n")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "stale.pyc").write_bytes(b"\x00\x00")
    bundled = root / "_bundled"
    bundled.mkdir()
    (bundled / "pyproject.toml").write_text(
        "[project]\nname = 'scitex-agent-container'\nversion = '0.0.0-test'\n"
        "\n[tool.hatch.build.targets.wheel]\n"
        "packages = ['src/scitex_agent_container']\n"
        '\n[tool.hatch.build.targets.wheel.hooks.custom]\npath = "scripts/hatch_build.py"\n'
    )
    (bundled / "README.md").write_text("# fake readme for tests\n")
    # The wheel force-includes the custom build hook too — pyproject
    # NAMES it, so a staged tree without it cannot be built at all.
    # The fixture must mirror the real wheel, or the suite is testing a
    # layout that does not ship.
    (bundled / "hatch_build.py").write_text("# fake build hook for tests\n")
    return root


@pytest.fixture
def fake_def(tmp_path: Path) -> Path:
    p = tmp_path / "apptainer-base.def"
    p.write_text(
        "Bootstrap: docker\nFrom: ubuntu:24.04\n%files\n    scitex-agent-container-src /opt/scitex-agent-container-src\n"
    )
    return p


@pytest.fixture(autouse=True)
def isolated_hermes_source_stager():
    """Keep unrelated image-build tests offline and deterministic."""
    saved = isb._stage_hermes_source
    saved_cards = isb._stage_cards_source
    isb._stage_hermes_source = lambda build_context: build_context
    isb._stage_cards_source = lambda build_context: build_context
    try:
        yield
    finally:
        isb._stage_hermes_source = saved
        isb._stage_cards_source = saved_cards


@contextmanager
def _use_container_build(build_fn) -> Iterator[list[tuple]]:
    """Swap ``_image_source_build._container_build`` for a real fake.

    ``_container_build`` is the seam that delegates to scitex-container's
    atomic ``build``; swapping it lets the unit tests exercise the
    staging + mapping without importing scitex-container or shelling a
    real apptainer. No MagicMock — a hand-rolled recording callable.
    """
    calls: list[tuple] = []

    def _recording(*a, **kw):
        calls.append((a, kw))
        return build_fn(*a, **kw)

    saved = isb._container_build
    isb._container_build = _recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        isb._container_build = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# stage_build_context
# ---------------------------------------------------------------------------


def test_source_provenance_accepts_loaded_package_root():
    # Arrange
    package_root = _LOADED_PACKAGE_ROOT
    # Act
    result = isb.assert_source_provenance(package_root)
    # Assert
    assert result is None


def test_environment_package_root_reads_editable_origin_from_selected_purelib(tmp_path):
    # Arrange
    repo = tmp_path / "selected-worktree"
    package = repo / "src" / "scitex_agent_container"
    package.mkdir(parents=True)
    purelib = tmp_path / "venv" / "site-packages"
    dist_info = purelib / "scitex_agent_container-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: scitex-agent-container\nVersion: 0.0.0\n"
    )
    (dist_info / "direct_url.json").write_text(
        '{"url":"' + repo.as_uri() + '","dir_info":{"editable":true}}'
    )
    # Act
    result = isb._environment_package_root(purelib)
    # Assert
    assert result == package


def test_source_provenance_accepts_symlink_to_loaded_package_root(tmp_path):
    # Arrange
    linked_root = tmp_path / "scitex_agent_container"
    linked_root.symlink_to(_LOADED_PACKAGE_ROOT, target_is_directory=True)
    # Act
    result = isb.assert_source_provenance(linked_root)
    # Assert
    assert result is None


def _capture_source_provenance_error(
    staged_root: Path, *, environment_root: Path | None = None
) -> str:
    try:
        isb.assert_source_provenance(staged_root, environment_root=environment_root)
    except isb.SourceProvenanceMismatch as exc:
        return str(exc)
    raise AssertionError("SourceProvenanceMismatch was not raised")


def test_source_provenance_refuses_mixed_root_and_names_both_paths(tmp_path):
    # Arrange
    staged_root = tmp_path / "other" / "scitex_agent_container"
    staged_root.mkdir(parents=True)
    # Act
    message = _capture_source_provenance_error(staged_root)
    # Assert
    assert (
        str(_LOADED_PACKAGE_ROOT) in message
        and str(staged_root.resolve()) in message
        and "PYTHONPATH=" in message
    )


def test_source_provenance_refuses_wrong_pythonpath_checkout_for_active_environment(
    tmp_path,
):
    # Arrange
    environment_root = tmp_path / "selected-worktree" / "src" / "scitex_agent_container"
    environment_root.mkdir(parents=True)
    # Act
    message = _capture_source_provenance_error(
        _LOADED_PACKAGE_ROOT, environment_root=environment_root
    )
    # Assert
    assert (
        f"loaded package root: {_LOADED_PACKAGE_ROOT}" in message
        and f"staged source root: {_LOADED_PACKAGE_ROOT}" in message
        and f"active-environment package root: {environment_root}" in message
    )


def test_stage_build_context_creates_dest_dir(tmp_path, fake_pkg_root, fake_def):
    # Arrange
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert
    assert dest.is_dir()


def test_stage_build_context_copies_def_under_its_original_name(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    dest = tmp_path / "staging"
    # Act
    staged_def = isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert
    assert staged_def == dest / "apptainer-base.def" and staged_def.is_file()


def test_stage_build_context_copies_package_source_under_fixed_name(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert — layout matches what `pip install <staged_src>` requires:
    # pyproject.toml at the staged root, package at src/<pkg-name>/
    src = dest / "scitex-agent-container-src"
    assert (
        src.is_dir()
        and (src / "pyproject.toml").is_file()
        and (src / "src" / "scitex_agent_container" / "__init__.py").is_file()
    )


def test_stage_build_context_excludes_pycache_from_staged_source(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert — __pycache__ inside the package must be filtered out
    pkg = dest / "scitex-agent-container-src" / "src" / "scitex_agent_container"
    assert not (pkg / "__pycache__").exists()


def test_stage_build_context_replaces_stale_generated_provenance_from_checkout(
    tmp_path, fake_def
):
    # Arrange — reproduce the real defect: clean current source bytes next to
    # a generated stamp left by an older build.  Apptainer removes .git, so
    # copying this stamp would make the inner wheel inherit the old commit.
    repo = tmp_path / "repo"
    package = repo / "src" / "scitex_agent_container"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'current'\n")
    bundled = package / "_bundled"
    bundled.mkdir()
    (bundled / "pyproject.toml").write_text(
        "[project]\nname = 'scitex-agent-container'\nversion = '9.8.7'\n"
        "\n[tool.hatch.build.targets.wheel]\n"
        "packages = ['src/scitex_agent_container']\n"
    )
    (bundled / "README.md").write_text("# test\n")
    (bundled / "hatch_build.py").write_text("# hook\n")
    old_stamp = stamp_path(package)
    old_stamp.parent.mkdir(parents=True)
    old_stamp.write_text(
        "STAMP = {'version': '0.0.1', 'commit': 'old-commit', "
        "'commit_source': 'git', 'code_hash': 'old-hash'}\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "current"], check=True)
    expected_commit = head_sha(repo)

    # Act
    dest = tmp_path / "staging"
    isb.stage_build_context(package, fake_def, dest)
    staged_package = (
        dest / "scitex-agent-container-src" / "src" / "scitex_agent_container"
    )
    staged = read_existing_stamp(staged_package)

    # Assert — source commit and staged bytes, never the copied old stamp.
    assert {
        "stamp_exists": staged is not None,
        "version": staged["version"],
        "commit": staged["commit"],
        "stale_commit_removed": staged["commit"] != "old-commit",
        "code_hash": staged["code_hash"],
        "stale_hash_removed": staged["code_hash"] != "old-hash",
    } == {
        "stamp_exists": True,
        "version": "9.8.7",
        "commit": expected_commit,
        "stale_commit_removed": True,
        "code_hash": code_hash(staged_package),
        "stale_hash_removed": True,
    }


def test_inner_wheel_build_inherits_fresh_staged_commit_and_hash(tmp_path, fake_def):
    # Arrange — the loaded package is a real clean checkout.  Staging must
    # leave enough provenance for the no-.git inner Apptainer wheel build.
    dest = tmp_path / "staging"
    isb.stage_build_context(_LOADED_PACKAGE_ROOT, fake_def, dest)
    staged_root = dest / "scitex-agent-container-src"
    staged_package = staged_root / "src" / "scitex_agent_container"
    staged = read_existing_stamp(staged_package)

    # Act — this is the same compute path hatch_build.py executes in %post.
    inherited = compute_stamp(
        staged_root,
        staged_package,
        version=staged["version"],
    )

    # Assert
    expected_commit = head_sha(_REPO_ROOT)
    assert (
        inherited["commit"],
        inherited["commit_source"],
        inherited["code_hash"],
    ) == (expected_commit, "inherited", code_hash(staged_package))


def test_stage_build_context_uses_bundled_pyproject_for_wheel_install(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — fake_pkg_root has _bundled/pyproject.toml (wheel layout)
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert — the staged pyproject.toml is the bundled copy verbatim
    staged_toml = dest / "scitex-agent-container-src" / "pyproject.toml"
    bundled = fake_pkg_root / "_bundled" / "pyproject.toml"
    assert staged_toml.read_text() == bundled.read_text()


def test_locate_bundled_pyproject_falls_back_to_editable_repo_root(tmp_path):
    # Arrange — editable install layout: <repo>/src/scitex_agent_container/
    # and <repo>/pyproject.toml at the repo root.
    repo = tmp_path / "repo"
    pkg = repo / "src" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")
    repo_toml = repo / "pyproject.toml"
    repo_toml.write_text("[project]\nname='x'\nversion='0.0.0'\n")
    # Act
    found = isb.locate_bundled_pyproject(pkg)
    # Assert
    assert found == repo_toml


def test_locate_bundled_readme_falls_back_to_editable_repo_root(tmp_path):
    # Arrange — same editable layout, asserting README.md is also found
    # at the repo root (paired with pyproject.toml's ``readme=`` claim).
    repo = tmp_path / "repo"
    pkg = repo / "src" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")
    repo_readme = repo / "README.md"
    repo_readme.write_text("# editable repo readme\n")
    # Act
    found = isb.locate_bundled_readme(pkg)
    # Assert
    assert found == repo_readme


def test_locate_bundled_hatch_build_falls_back_to_editable_src_dir(tmp_path):
    # Arrange — editable layout. hatch_build.py lives at <repo>/scripts/,
    # NOT the repo root: it is the one bundled sibling whose repo path
    # differs from its slot in the wheel's flat _bundled/ dir.
    repo = tmp_path / "repo"
    pkg = repo / "src" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")
    (repo / "scripts").mkdir(parents=True)
    hook = repo / "scripts" / "hatch_build.py"
    hook.write_text("# editable repo build hook\n")
    # Act
    found = isb.locate_bundled_hatch_build(pkg)
    # Assert
    assert found == hook


def test_locate_bundled_hatch_build_raises_when_neither_location_exists(tmp_path):
    # Arrange — no _bundled/hatch_build.py and no <repo>/scripts/hatch_build.py
    pkg = tmp_path / "orphan-pkg" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")

    # Act
    def _call():
        return isb.locate_bundled_hatch_build(pkg)

    # Assert
    with pytest.raises(FileNotFoundError, match=r"could not locate hatch_build\.py"):
        _call()


def test_stage_build_context_stages_hatch_build_hook_under_src(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — pyproject declares hooks.custom path = "scripts/hatch_build.py",
    # a path hatchling resolves against the STAGED root.
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert — the hook is staged at exactly the path pyproject names,
    # beside (not inside) the package dir.
    staged_hook = dest / "scitex-agent-container-src" / "scripts" / "hatch_build.py"
    bundled = fake_pkg_root / "_bundled" / "hatch_build.py"
    assert staged_hook.is_file() and staged_hook.read_text() == bundled.read_text()


def test_stage_build_context_missing_hatch_build_raises(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — a wheel-shaped pkg root whose _bundled/ has pyproject +
    # README but NOT the build hook. That is precisely the layout every
    # sac wheel shipped before this fix, and it must now fail LOUD at
    # staging rather than silently producing a tree the container's
    # `uv pip install` dies on 8 minutes into %post.
    (fake_pkg_root / "_bundled" / "hatch_build.py").unlink()
    dest = tmp_path / "staging"

    # Act
    def _call():
        return isb.stage_build_context(fake_pkg_root, fake_def, dest)

    # Assert
    with pytest.raises(FileNotFoundError, match=r"could not locate hatch_build\.py"):
        _call()


def test_stage_build_context_does_not_wipe_dest_when_hatch_build_missing(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — the hook is resolved BEFORE dest_dir is wiped, so a
    # missing hook must not strand the operator with a half-staged tree.
    (fake_pkg_root / "_bundled" / "hatch_build.py").unlink()
    dest = tmp_path / "staging"
    dest.mkdir()
    sentinel = dest / "preexisting.txt"
    sentinel.write_text("must not be wiped\n")

    # Act
    try:
        isb.stage_build_context(fake_pkg_root, fake_def, dest)
    except FileNotFoundError:
        pass

    # Assert
    assert sentinel.read_text() == "must not be wiped\n"


def test_locate_bundled_pyproject_raises_when_neither_location_exists(tmp_path):
    # Arrange — a pkg root with no _bundled/ and no usable repo-root sibling
    pkg = tmp_path / "orphan-pkg" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")

    # Act
    def _call():
        return isb.locate_bundled_pyproject(pkg)

    # Assert
    with pytest.raises(FileNotFoundError, match=r"could not locate pyproject\.toml"):
        _call()


def test_locate_bundled_readme_raises_when_neither_location_exists(tmp_path):
    # Arrange — a pkg root with no _bundled/ and no usable repo-root sibling
    pkg = tmp_path / "orphan-pkg" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")

    # Act
    def _call():
        return isb.locate_bundled_readme(pkg)

    # Assert
    with pytest.raises(FileNotFoundError, match=r"could not locate README\.md"):
        _call()


def test_stage_build_context_copies_readme_alongside_pyproject(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — the fake fixture ships a bundled README.md
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert — README.md sits at the staged-source root, same dir as
    # pyproject.toml, so hatchling can read it during pip install.
    staged_readme = dest / "scitex-agent-container-src" / "README.md"
    bundled = fake_pkg_root / "_bundled" / "README.md"
    assert staged_readme.is_file() and staged_readme.read_text() == bundled.read_text()


def test_stage_build_context_missing_pyproject_raises(tmp_path, fake_def):
    # Arrange — a pkg root without _bundled/pyproject.toml and no
    # editable repo root sibling.
    pkg = tmp_path / "orphan-pkg" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")
    dest = tmp_path / "staging"

    # Act
    def _call():
        return isb.stage_build_context(pkg, fake_def, dest)

    # Assert
    with pytest.raises(FileNotFoundError):
        _call()


def test_stage_build_context_does_not_wipe_dest_when_pyproject_missing(
    tmp_path, fake_def
):
    # Arrange — same orphan-pkg setup, but seed dest with an operator
    # sentinel that must survive the failed staging attempt.
    pkg = tmp_path / "orphan-pkg" / "scitex_agent_container"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("\n")
    dest = tmp_path / "staging"
    dest.mkdir()
    sentinel = dest / "preexisting.txt"
    sentinel.write_text("must not be wiped\n")

    # Act — let the FileNotFoundError propagate; we only care about
    # the side effect on disk.
    try:
        isb.stage_build_context(pkg, fake_def, dest)
    except FileNotFoundError:
        pass

    # Assert
    assert sentinel.read_text() == "must not be wiped\n"


def test_stage_build_context_resets_prior_staging_dir(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — a stale file inside the staging dir from a prior failed run
    dest = tmp_path / "staging"
    dest.mkdir()
    (dest / "stale-leftover.txt").write_text("stale\n")
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert
    assert not (dest / "stale-leftover.txt").exists()


def test_stage_build_context_missing_def_raises_FileNotFoundError(
    tmp_path, fake_pkg_root
):
    # Arrange
    dest = tmp_path / "staging"

    # Act
    def _call():
        return isb.stage_build_context(fake_pkg_root, tmp_path / "no-such.def", dest)

    # Assert
    with pytest.raises(FileNotFoundError):
        _call()


def test_stage_build_context_missing_pkg_root_raises_FileNotFoundError(
    tmp_path, fake_def
):
    # Arrange
    dest = tmp_path / "staging"

    # Act
    def _call():
        return isb.stage_build_context(tmp_path / "no-pkg-root", fake_def, dest)

    # Assert
    with pytest.raises(FileNotFoundError):
        _call()


def test_stage_build_context_pkg_root_is_file_raises_NotADirectoryError(
    tmp_path, fake_def
):
    # Arrange
    bad_root = tmp_path / "not-a-dir"
    bad_root.write_text("nope\n")
    dest = tmp_path / "staging"

    # Act
    def _call():
        return isb.stage_build_context(bad_root, fake_def, dest)

    # Assert
    with pytest.raises(NotADirectoryError):
        _call()


# ---------------------------------------------------------------------------
# stage_build_context — bootstrap_sif (layered .def support)
# ---------------------------------------------------------------------------


def test_stage_build_context_symlinks_bootstrap_sif_when_provided(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — layered .def case: a prerequisite SIF must be staged
    # next to the .def so apptainer's relative ``From: ./<name>.sif``
    # resolves at build time. We use a small fake file; the helper
    # symlinks (not copies) for instant staging of the real ~3 GB
    # base SIF.
    dest = tmp_path / "staging"
    fake_base_sif = tmp_path / "sac-base.sif"
    fake_base_sif.write_bytes(b"fake SIF bytes")
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest, bootstrap_sif=fake_base_sif)
    # Assert — staged entry is a symlink (not a copy) pointing at the
    # absolute resolved source-SIF path; survives the dest_dir's
    # rmtree-on-next-build lifecycle because the target lives outside.
    staged = dest / "sac-base.sif"
    assert staged.is_symlink() and Path(os.readlink(staged)) == fake_base_sif.resolve()


def test_stage_build_context_omits_bootstrap_sif_entry_when_not_provided(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — top-of-stack .def (``base``, ``proxy``) has no
    # prerequisite SIF. The staging dir must NOT carry a stray .sif
    # entry that a non-layered build could trip over.
    dest = tmp_path / "staging"
    # Act
    isb.stage_build_context(fake_pkg_root, fake_def, dest)
    # Assert
    sifs = list(dest.glob("*.sif"))
    assert sifs == []


def test_stage_build_context_raises_when_bootstrap_sif_missing(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — the operator asked for a layered build but the
    # prerequisite SIF was never produced (e.g. forgot ``sac image
    # build base`` first). The helper must FAIL LOUD with a message
    # that names the missing path AND explains the fix — not let
    # apptainer crash mid-build on a half-staged context (the
    # 2026-06-07 cohort-A rebuild stall).
    dest = tmp_path / "staging"
    ghost_sif = tmp_path / "does-not-exist.sif"

    # Act
    def _call():
        isb.stage_build_context(fake_pkg_root, fake_def, dest, bootstrap_sif=ghost_sif)

    # Assert
    with pytest.raises(FileNotFoundError, match=r"bootstrap SIF not found"):
        _call()


def test_stage_build_context_preserves_staging_dir_when_bootstrap_sif_missing(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — when the bootstrap-sif check fails, the staging dir
    # must NOT have been wiped. Pairs with the resolve-before-wipe
    # rule already covering pyproject.toml / README.md so prior good
    # state survives a misconfig.
    dest = tmp_path / "staging"
    dest.mkdir()
    sentinel = dest / "operator-state.txt"
    sentinel.write_text("must survive a failed stage_build_context call\n")
    ghost_sif = tmp_path / "does-not-exist.sif"

    # Act
    try:
        isb.stage_build_context(fake_pkg_root, fake_def, dest, bootstrap_sif=ghost_sif)
    except FileNotFoundError:
        pass

    # Assert
    assert sentinel.is_file()


# ---------------------------------------------------------------------------
# build_layer_from_source
# ---------------------------------------------------------------------------


def _stub_build_result(*_a, output_dir, image_name, sandbox, **_kw) -> Path:
    """A fake ``_container_build`` that mimics scitex-container's returns.

    SIF build → the resolved timestamped real SIF
    (``<output_dir>/<image_name>/<image_name>-<ts>.sif``). Sandbox build →
    the sandbox dir. ``build_layer_from_source`` derives the STABLE inner
    boot symlink from ``output_dir`` + ``image_name`` itself, so the exact
    timestamp here is irrelevant to what the helper returns.
    """
    image_dir = Path(output_dir) / image_name
    if sandbox:
        return image_dir / f"{image_name}.sandbox"
    return image_dir / f"{image_name}-20260702T000000Z.sif"


def test_build_layer_from_source_invokes_build_with_staging_cwd(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result) as calls:
        isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert
    kwargs = calls[0][1]
    expected_cwd = out_dir / "sac-base" / "build-context"
    assert kwargs["cwd"] == expected_cwd and expected_cwd.is_dir()


def test_build_layer_from_source_maps_output_dir_and_image_name(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — the containers output dir maps straight to ``output_dir``;
    # the layer maps to ``image_name`` (``sac-<layer>``) so scitex-
    # container lands the artefact under ``<output_dir>/sac-<layer>/``.
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result) as calls:
        isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert
    kwargs = calls[0][1]
    assert kwargs["output_dir"] == out_dir and kwargs["image_name"] == "sac-base"


def test_build_layer_from_source_passes_staged_def_and_force(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — ``def_path`` is the STAGED copy (in the build-context
    # dir), not the source .def, so scitex-container resolves relative
    # ``%files`` against the same staging dir it uses as ``cwd``.
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result) as calls:
        isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
            force=True,
        )
    # Assert
    kwargs = calls[0][1]
    staged_def = out_dir / "sac-base" / "build-context" / "apptainer-base.def"
    assert (
        kwargs["def_path"] == staged_def
        and kwargs["force"] is True
        and kwargs["sandbox"] is False
    )


def test_build_layer_from_source_omits_retain_for_config_default(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — sac does not pin ``retain``; scitex-container falls back
    # to the image config's retention default (keeps N previous + live).
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result) as calls:
        isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert
    assert "retain" not in calls[0][1]


def test_build_layer_from_source_returns_stable_inner_boot_symlink(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — build() returns a timestamped real SIF; the helper must
    # return the STABLE inner boot symlink (layout-invariant across
    # rebuilds), which callers + the next layer's bootstrap resolve.
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result):
        result = isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert
    assert result == out_dir / "sac-base" / "sac-base.sif"


def test_build_layer_from_source_sandbox_returns_sandbox_path(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"
    # Act
    with _use_container_build(_stub_build_result):
        result = isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
            sandbox=True,
        )
    # Assert
    assert result == out_dir / "sac-base" / "sac-base.sandbox"


def test_build_layer_from_source_propagates_runtime_error(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — scitex-container's ``build`` raises RuntimeError on a
    # failed apptainer build; the helper must let it propagate (the live
    # image + symlinks are left intact by the atomic build).
    out_dir = tmp_path / "out"

    def _raising(*_a, **_kw):
        raise RuntimeError("apptainer build failed; see build log")

    # Act
    def _call():
        with _use_container_build(_raising):
            isb.build_layer_from_source(
                layer="base",
                def_path=fake_def,
                pkg_root=fake_pkg_root,
                output_dir=out_dir,
            )

    # Assert
    with pytest.raises(RuntimeError, match=r"apptainer build failed"):
        _call()


def test_build_layer_from_source_stages_source_at_known_relative_name(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — build() records cwd; we then check the staged tree on disk.
    out_dir = tmp_path / "out"
    seen_cwds: list[Path] = []

    def _record_cwd(*_a, cwd, output_dir, image_name, sandbox, **_kw):
        seen_cwds.append(cwd)
        return _stub_build_result(
            output_dir=output_dir, image_name=image_name, sandbox=sandbox
        )

    # Act
    with _use_container_build(_record_cwd):
        isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert — the .def's %files reference resolves under cwd to the
    # bundled, pip-installable source tree (pyproject.toml at the root,
    # package at src/scitex_agent_container/).
    staged_src = seen_cwds[0] / "scitex-agent-container-src"
    assert (
        staged_src.is_dir()
        and (staged_src / "pyproject.toml").is_file()
        and (staged_src / "src" / "scitex_agent_container" / "__init__.py").is_file()
    )


def test_base_build_stages_hermes_source_in_same_build_context(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    staged: list[Path] = []
    saved = isb._stage_hermes_source
    isb._stage_hermes_source = lambda path: staged.append(path)
    out_dir = tmp_path / "out"

    # Act
    try:
        with _use_container_build(_stub_build_result):
            isb.build_layer_from_source(
                layer="base",
                def_path=fake_def,
                pkg_root=fake_pkg_root,
                output_dir=out_dir,
            )
    finally:
        isb._stage_hermes_source = saved

    # Assert
    assert staged == [out_dir / "sac-base" / "build-context"]


def test_cards_runtime_layers_stage_cards_source_in_same_build_context(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    staged: list[Path] = []
    saved = isb._stage_cards_source
    isb._stage_cards_source = lambda path: staged.append(path)
    out_dir = tmp_path / "out"

    # Act
    try:
        with _use_container_build(_stub_build_result):
            for layer in ("base", "scitex"):
                isb.build_layer_from_source(
                    layer=layer,
                    def_path=fake_def,
                    pkg_root=fake_pkg_root,
                    output_dir=out_dir,
                )
    finally:
        isb._stage_cards_source = saved

    # Assert
    assert staged == [
        out_dir / "sac-base" / "build-context",
        out_dir / "sac-scitex" / "build-context",
    ]


def test_non_base_build_does_not_stage_hermes_source(tmp_path, fake_pkg_root, fake_def):
    # Arrange
    staged: list[Path] = []
    saved = isb._stage_hermes_source
    isb._stage_hermes_source = lambda path: staged.append(path)

    # Act
    try:
        with _use_container_build(_stub_build_result):
            isb.build_layer_from_source(
                layer="proxy",
                def_path=fake_def,
                pkg_root=fake_pkg_root,
                output_dir=tmp_path / "out",
            )
    finally:
        isb._stage_hermes_source = saved

    # Assert
    assert staged == []


def test_build_layer_from_source_forwards_bootstrap_sif_to_staging(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — pin that the public builder forwards ``bootstrap_sif``
    # through to ``stage_build_context`` so the staged context (== the
    # ``cwd`` build context) for a layered .def carries the prerequisite
    # SIF. Without this, apptainer would FATAL on a half-staged context
    # (the 2026-06-07 cohort-A rebuild stall). This is exactly how the
    # layered ``From: ./sac-base.sif`` resolves: the symlink lives in the
    # ``cwd`` staging dir scitex-container's ``build`` runs against.
    out_dir = tmp_path / "out"
    fake_base_sif = tmp_path / "sac-base.sif"
    fake_base_sif.write_bytes(b"fake SIF")

    # Act
    with _use_container_build(_stub_build_result):
        isb.build_layer_from_source(
            layer="scitex",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
            bootstrap_sif=fake_base_sif,
        )

    # Assert — the staging dir for the scitex layer now carries the
    # bootstrap SIF as a sibling of the staged .def, ready for
    # apptainer's relative ``From: ./sac-base.sif`` to resolve.
    staged_base = out_dir / "sac-scitex" / "build-context" / "sac-base.sif"
    assert staged_base.exists() and staged_base.is_symlink()


# ---------------------------------------------------------------------------
# resolve_bootstrap_sif — layer → prerequisite SIF policy
# ---------------------------------------------------------------------------


def test_resolve_bootstrap_sif_base_returns_none(tmp_path):
    # Arrange — top-of-stack ``base`` has no prerequisite SIF.
    out_dir = tmp_path / "out"
    # Act
    result = isb.resolve_bootstrap_sif("base", out_dir)
    # Assert
    assert result is None


def test_resolve_bootstrap_sif_scitex_returns_inner_base_boot_symlink(tmp_path):
    # Arrange — under 0.3.0's atomic layout the prerequisite is the STABLE
    # inner boot symlink ``<out>/sac-base/sac-base.sif`` (a symlink to the
    # live timestamped SIF). ``is_file()`` follows it, so a valid symlink
    # resolves. Model that: a real timestamped SIF + a symlink to it.
    out_dir = tmp_path / "out"
    base_dir = out_dir / "sac-base"
    base_dir.mkdir(parents=True)
    real_sif = base_dir / "sac-base-20260702T000000Z.sif"
    real_sif.write_bytes(b"fake base SIF")
    inner = base_dir / "sac-base.sif"
    inner.symlink_to(real_sif.name)
    # Act
    result = isb.resolve_bootstrap_sif("scitex", out_dir)
    # Assert
    assert result == inner


def test_resolve_bootstrap_sif_scitex_raises_when_base_missing(tmp_path):
    # Arrange — scitex requested but sac-base.sif was never built.
    out_dir = tmp_path / "out"

    # Act
    def _call():
        return isb.resolve_bootstrap_sif("scitex", out_dir)

    # Assert
    with pytest.raises(isb.BootstrapSifMissing, match=r"sac image build base"):
        _call()


# ---------------------------------------------------------------------------
# Shipped .def contract — every apptainer-*.def installs sac from the
# bundled source, never via ``git+...``. Co-located here (vs a separate
# tests/scitex_agent_container/containers/ file) to satisfy PS-204 §2:
# every test file must mirror exactly one src file. The helper module
# under test (_image_source_build.py) owns the staging contract these
# .def files participate in, so this is the natural home.
# ---------------------------------------------------------------------------


# All three shipped .defs — base/scitex/proxy. _LAYERS only covers
# base+scitex (those are what ``sac image build`` accepts); proxy is
# built by other paths but the same source-bundled invariant applies.
_ALL_DEF_NAMES = sorted(set(_LAYERS.values()) | {"apptainer-proxy.def"})


def _uv_pip_install_commands(recipe: str) -> list[list[str]]:
    """Tokenize real backslash-continued ``uv pip install`` commands."""
    commands: list[list[str]] = []
    lines = iter(recipe.splitlines())
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("uv pip install"):
            continue
        parts = [stripped]
        while parts[-1].endswith("\\"):
            parts.append(next(lines).strip())
        command = " ".join(part.removesuffix("\\") for part in parts)
        commands.append(shlex.split(command))
    return commands


@pytest.fixture
def def_text(request) -> str:
    """Read one .def file's text by its bare filename."""
    name: str = request.param
    path = _RECIPES_DIR / name
    assert path.is_file(), f"shipped recipe missing: {path}"
    return path.read_text()


@pytest.mark.parametrize("def_text", _ALL_DEF_NAMES, indirect=True)
def test_def_has_files_section_copying_bundled_source(def_text: str):
    # Arrange — the .def declares the %files entry the staging helper depends on
    expected = f"{isb._STAGED_SRC_NAME} /opt/scitex-agent-container-src"
    # Act
    present = expected in def_text
    # Assert
    assert present, (
        f".def is missing the bundled-source %files entry "
        f"'{expected}'. The staging helper stages the source under that "
        f"name; .def must reference it verbatim."
    )


@pytest.mark.parametrize("def_text", _ALL_DEF_NAMES, indirect=True)
def test_def_installs_sac_from_bundled_source_absolute_path(def_text: str):
    # Arrange — %post installs from the bundled source path — not from git
    expected = "/opt/scitex-agent-container-src"
    # Act
    present = expected in def_text
    # Assert
    assert present, (
        ".def must `pip install /opt/scitex-agent-container-src` in %post "
        "so the in-SIF sac is structurally pinned to the source tree "
        "that shipped this .def."
    )


@pytest.mark.parametrize("def_text", _ALL_DEF_NAMES, indirect=True)
def test_def_does_not_install_sac_via_git_ref(def_text: str):
    # Arrange — banned substrings; any of these drifts from the source tree
    banned_substrings = (
        "git+https://github.com/ywatanabe1989/scitex-agent-container",
        "git+ssh://git@github.com/ywatanabe1989/scitex-agent-container",
        "@develop",
        "@v0.14.0",
    )
    # Act
    offenders = [b for b in banned_substrings if b in def_text]
    # Assert
    assert offenders == [], (
        f".def must not reference any of {offenders} for the sac install — "
        f"use the bundled source at /opt/scitex-agent-container-src so the "
        f"SIF's sac is always exactly the source that shipped the .def."
    )


def test_base_def_tests_installed_sac_console_before_publish():
    # Arrange
    recipe = (_RECIPES_DIR / "apptainer-base.def").read_text()
    test_section = recipe.partition("%test")[2].partition("%labels")[0]
    # Act
    command_present = "/opt/venv-sac/bin/sac --version" in test_section
    # Assert
    assert command_present is True


def test_recipes_dir_holds_all_three_shipped_defs():
    # Arrange — declared expected set
    expected = set(_ALL_DEF_NAMES)
    # Act
    actual = sorted(p.name for p in _RECIPES_DIR.glob("apptainer-*.def"))
    # Assert
    assert expected.issubset(set(actual)), (
        f"missing one or more shipped .def files. expected at least "
        f"{_ALL_DEF_NAMES}, found {actual}"
    )


def test_def_files_use_consistent_staged_source_name():
    # Arrange — all three .defs must agree on the path inside the image
    names = _ALL_DEF_NAMES
    # Act
    missing = [
        name
        for name in names
        if (_RECIPES_DIR / name).read_text().count("/opt/scitex-agent-container-src")
        < 1
    ]
    # Assert
    assert missing == [], (
        f"{missing} do not reference /opt/scitex-agent-container-src; "
        f"layered .defs must agree on the in-image source path."
    )


def test_real_uv_install_commands_do_not_repeat_singleton_flags():
    # Arrange — uv's clap parser rejects repeated set-once flags before it
    # resolves anything. Parse the shipped recipes' complete continuation
    # blocks: checking isolated lines or substring presence missed this exact
    # failure when --no-deps appeared on both lines of one command.
    singleton_flags = {"--no-cache", "--no-deps", "--python", "-U", "--upgrade"}
    offenders: list[str] = []

    # Act
    for name in _ALL_DEF_NAMES:
        recipe = (_RECIPES_DIR / name).read_text()
        for argv in _uv_pip_install_commands(recipe):
            duplicates = sorted(
                flag for flag in singleton_flags if argv.count(flag) > 1
            )
            if duplicates:
                offenders.append(f"{name}: {duplicates}: {shlex.join(argv)}")

    # Assert
    assert offenders == [], "duplicate singleton uv flags:\n" + "\n".join(offenders)


def test_staged_src_name_matches_def_files_files_entry():
    # Arrange — the contract: every .def declares _STAGED_SRC_NAME in %files
    names = _ALL_DEF_NAMES
    # Act
    missing = [
        name
        for name in names
        if f"\n    {isb._STAGED_SRC_NAME} " not in (_RECIPES_DIR / name).read_text()
    ]
    # Assert
    assert missing == [], (
        f"{missing} must declare '{isb._STAGED_SRC_NAME}' as a %files source "
        f"so the staging helper's copy is what gets bundled."
    )


# ---------------------------------------------------------------------------
# Wheel-ships contract — the built wheel must contain pyproject.toml +
# README.md under ``scitex_agent_container/_bundled/`` so the helper's
# wheel-install branch of locate_bundled_pyproject/_readme finds them.
# Co-located here for the same PS-204 §2 reason as the .def contract.
# Skipped when the wheel-build toolchain (pip) is not importable —
# CI runners always have pip; minimal container test images may not.
# ---------------------------------------------------------------------------


_PKG_PATH = Path(scitex_agent_container.__file__).resolve()
# parents[0] -> scitex_agent_container/   parents[1] -> src/   parents[2] -> <repo>/
_REPO_ROOT = _PKG_PATH.parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_HAVE_PIP = True
try:
    import pip  # noqa: F401
except ImportError:
    _HAVE_PIP = False

# Per-test skips applied via decorator below — we deliberately avoid
# module-level `pytestmark` so the staging-helper + .def-contract tests
# above keep running on minimal images.
_skip_no_repo = pytest.mark.skipif(
    not _PYPROJECT.is_file(),
    reason="wheel-build smoke requires editable install with repo root on disk",
)
_skip_no_pip = pytest.mark.skipif(
    not _HAVE_PIP,
    reason="wheel-build smoke requires pip; CI runners have it, minimal "
    "container images may not",
)


# ---------------------------------------------------------------------------
# Staged-tree completeness — THE GUARD THAT WAS MISSING.
#
# Every staging test above runs against ``fake_pkg_root``, whose fake
# pyproject.toml declares nothing. So when #652 added a custom hatchling
# build hook to the REAL pyproject and nobody taught the stager to copy
# it, the whole suite stayed green while EVERY `sac image build` died in
# %post — 8 minutes in, on a machine nobody was watching:
#
#     OSError: Build script does not exist: scripts/hatch_build.py
#
# A fixture that declares nothing cannot disagree with a stager that
# copies nothing. So this test stages the REAL package root and asserts
# the general invariant, derived FROM the staged pyproject rather than
# hardcoded: every path pyproject NAMES must EXIST in the staged tree.
# It fails on any future pyproject that names a file staging forgets —
# not just on hatch_build.py.
# ---------------------------------------------------------------------------


def _declared_hook_paths(pyproject_path: Path) -> list[str]:
    """Every ``hooks.*.path`` the pyproject declares, across all targets.

    hatchling resolves each of these RELATIVE TO THE TREE BEING BUILT,
    which for a SIF build is the staged tree — so each one is a file the
    stager owes the build.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    targets = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    paths: list[str] = []
    for target in targets.values():
        for hook in (target.get("hooks") or {}).values():
            path = (hook or {}).get("path")
            if path:
                paths.append(path)
    return paths


def _declared_wheel_packages(pyproject_path: Path) -> list[str]:
    """Return every source package the staged wheel configuration names."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]


@pytest.fixture(scope="module")
def real_staged_src(tmp_path_factory) -> Path:
    """Stage the REAL package root + a REAL shipped .def, once.

    Exactly what ``sac image build`` stages before it hands the tree to
    apptainer — no fakes, so the staged pyproject is the one that ships.
    """
    dest = tmp_path_factory.mktemp("real-build-context") / "build-context"
    isb.stage_build_context(_PKG_PATH.parent, _RECIPES_DIR / "apptainer-base.def", dest)
    return dest / isb._STAGED_SRC_NAME


@_skip_no_repo
def test_real_staged_pyproject_declares_at_least_one_build_hook(real_staged_src: Path):
    # Arrange — if the real pyproject declared no hooks, the completeness
    # test below would pass VACUOUSLY. Pin that it cannot.
    pyproject = real_staged_src / "pyproject.toml"
    # Act
    declared = _declared_hook_paths(pyproject)
    # Assert
    assert declared, (
        "the staged pyproject declares no hatchling build hooks at all, so "
        "the staged-tree completeness guard would pass vacuously. Either "
        "staging copied the wrong pyproject, or the hook was removed — in "
        "which case delete the guard deliberately rather than let it rot "
        "into a test that can never fail."
    )


@_skip_no_repo
def test_staged_tree_contains_every_path_the_real_pyproject_declares(
    real_staged_src: Path,
):
    # Arrange — every path the STAGED pyproject names. Derived, not
    # hardcoded: this fails for any future pyproject that names a file
    # staging forgets, not just for hatch_build.py.
    declared = _declared_hook_paths(real_staged_src / "pyproject.toml")
    # Act — check each against the STAGED tree
    missing = [p for p in declared if not (real_staged_src / p).is_file()]
    # Assert
    assert missing == [], (
        f"the staged source tree is missing {missing}, which the staged "
        f"pyproject.toml NAMES as a hatchling build-hook path. hatchling "
        f"resolves those against the tree being built, so the .def's "
        f"`uv pip install /opt/scitex-agent-container-src` dies in %post "
        f"with 'Build script does not exist: {missing[0] if missing else ''}' "
        f"— minutes into a SIF build nobody is watching. Stage every file "
        f"pyproject names, not just the package."
    )


@_skip_no_repo
def test_staged_tree_contains_every_declared_wheel_package(real_staged_src: Path):
    # Arrange
    declared = _declared_wheel_packages(real_staged_src / "pyproject.toml")
    # Act
    missing = [path for path in declared if not (real_staged_src / path).is_dir()]
    # Assert
    assert missing == [], (
        f"the staged source tree is missing declared wheel packages {missing}; "
        "the SIF's source install would create console entry points whose target "
        "modules do not exist"
    )


def _build_wheel(out_dir: Path, source: Path = _REPO_ROOT) -> Path:
    """Build the wheel + return the .whl path. ``pip wheel --no-deps``.

    ``pip wheel --no-deps`` drives the PEP 517 backend (hatchling) to
    produce the wheel without resolving runtime deps — fast enough for
    a CI smoke and doesn't require the optional ``build`` frontend.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(out_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"wheel build failed (rc={result.returncode}); skipping "
            f"force-include smoke. stderr tail: {result.stderr[-400:]}"
        )
    wheels = list(out_dir.glob("scitex_agent_container-*.whl"))
    assert wheels, f"no wheel produced in {out_dir}"
    return wheels[0]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("wheel-out")
    return _build_wheel(out_dir)


@pytest.fixture(scope="module")
def staged_wheel(tmp_path_factory, real_staged_src: Path) -> Path:
    """Build the same source tree copied into the SIF's ``/opt`` path."""
    out_dir = tmp_path_factory.mktemp("staged-wheel-out")
    return _build_wheel(out_dir, real_staged_src)


@_skip_no_repo
@_skip_no_pip
def test_sif_staged_wheel_contains_importable_console_bootstrap(staged_wheel: Path):
    # Arrange
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(staged_wheel)!r}); "
        "import _scitex_agent_container_bootstrap as bootstrap; "
        "assert bootstrap.__file__"
    )
    # Act
    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
    )
    # Assert
    assert result.returncode == 0, result.stderr


@_skip_no_repo
@_skip_no_pip
def test_wheel_ships_bundled_hatch_build_py(built_wheel: Path):
    # Arrange — the bundled pyproject NAMES scripts/hatch_build.py as a build
    # hook, so the wheel must carry the hook too or a wheel-installed sac
    # stages a pyproject whose hook it does not have. That is the FLEET
    # case: agents run sac from a wheel, not a checkout.
    expected = "scitex_agent_container/_bundled/hatch_build.py"
    # Act
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    # Assert
    assert expected in names, (
        f"wheel must ship hatch_build.py under {expected} (force-include in "
        "pyproject.toml). Ship the bundled pyproject without the hook it "
        "declares and every `sac image build` from a wheel-installed sac "
        "dies in %post: 'Build script does not exist: scripts/hatch_build.py'."
    )


@_skip_no_repo
@_skip_no_pip
def test_wheel_does_not_ship_hatch_build_as_importable_module(built_wheel: Path):
    # Arrange — hatch_build.py imports hatchling at top level, so it must
    # never land on the RUNTIME import path. _bundled/ is inert data (no
    # __init__.py); scitex_agent_container/hatch_build.py would not be.
    forbidden = "scitex_agent_container/hatch_build.py"
    # Act
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    # Assert
    assert forbidden not in names, (
        f"{forbidden} must not ship: it imports hatchling at module level "
        "and the build frontend does not exist at runtime. Bundle it as "
        "inert data under _bundled/ instead. Bundled != packaged."
    )


@_skip_no_repo
@_skip_no_pip
def test_wheel_ships_bundled_pyproject_toml(built_wheel: Path):
    # Arrange
    expected = "scitex_agent_container/_bundled/pyproject.toml"
    # Act
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    # Assert
    assert expected in names, (
        f"wheel must ship pyproject.toml under {expected} (force-include "
        "in pyproject.toml). The source-bundled SIF build's "
        "locate_bundled_pyproject() looks there on wheel installs; "
        "missing it would break sac image build for wheel users."
    )


@_skip_no_repo
@_skip_no_pip
def test_wheel_ships_bundled_readme_md(built_wheel: Path):
    # Arrange
    expected = "scitex_agent_container/_bundled/README.md"
    # Act
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    # Assert
    assert expected in names, (
        f"wheel must ship README.md under {expected} (force-include in "
        "pyproject.toml). hatchling needs it to build the staged source "
        "tree because pyproject's ``readme = 'README.md'`` resolves "
        "alongside pyproject.toml at install time."
    )


@_skip_no_repo
@_skip_no_pip
def test_bundled_pyproject_in_wheel_matches_repo_root(built_wheel: Path):
    # Arrange
    repo_text = _PYPROJECT.read_text()
    # Act
    with zipfile.ZipFile(built_wheel) as zf:
        bundled_text = zf.read("scitex_agent_container/_bundled/pyproject.toml").decode(
            "utf-8"
        )
    # Assert — force-include must copy bytes verbatim. If hatchling
    # ever applies templating we'd want to know.
    assert bundled_text == repo_text, (
        "bundled pyproject.toml diverged from repo-root pyproject.toml; "
        "the staged SIF source tree would then build a different package "
        "than the one that shipped the .def."
    )
