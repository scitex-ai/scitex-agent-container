"""Tests for ``cli_pkg/_image_source_build.py``.

Covers the staging helper itself plus two integration contracts the
helper depends on (consolidated here, not in a separate test file, to
satisfy PS-204 §2 orphan-test-file: every test file must mirror a
single src file):

  - ``stage_build_context`` / ``build_layer_from_source`` /
    ``_default_apptainer_build_runner`` — unit coverage of the helper.
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

import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import scitex_agent_container
from scitex_agent_container.cli_pkg import _image_source_build as isb
from scitex_agent_container.cli_pkg.image_group import _LAYERS, _RECIPES_DIR

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
    )
    (bundled / "README.md").write_text("# fake readme for tests\n")
    return root


@pytest.fixture
def fake_def(tmp_path: Path) -> Path:
    p = tmp_path / "apptainer-base.def"
    p.write_text(
        "Bootstrap: docker\nFrom: ubuntu:24.04\n%files\n    scitex-agent-container-src /opt/scitex-agent-container-src\n"
    )
    return p


@contextmanager
def _use_apptainer_runner(runner_fn) -> Iterator[list[tuple]]:
    """Swap ``_image_source_build._apptainer_build_runner`` for a real fake."""
    calls: list[tuple] = []

    def _recording(*a, **kw):
        calls.append((a, kw))
        return runner_fn(*a, **kw)

    saved = isb._apptainer_build_runner
    isb._apptainer_build_runner = _recording  # type: ignore[assignment]
    try:
        yield calls
    finally:
        isb._apptainer_build_runner = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# stage_build_context
# ---------------------------------------------------------------------------


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
# build_layer_from_source
# ---------------------------------------------------------------------------


def test_build_layer_from_source_invokes_runner_with_staging_cwd(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"
    # Act
    with _use_apptainer_runner(lambda *a, **kw: 0) as calls:
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


def test_build_layer_from_source_writes_output_into_layer_dir(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"
    # Act
    with _use_apptainer_runner(lambda *a, **kw: 0):
        result = isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
        )
    # Assert
    assert result == out_dir / "sac-base" / "sac-base.sif"


def test_build_layer_from_source_sandbox_writes_sandbox_path(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"
    # Act
    with _use_apptainer_runner(lambda *a, **kw: 0):
        result = isb.build_layer_from_source(
            layer="base",
            def_path=fake_def,
            pkg_root=fake_pkg_root,
            output_dir=out_dir,
            sandbox=True,
        )
    # Assert
    assert result == out_dir / "sac-base" / "sac-base.sandbox"


def test_build_layer_from_source_raises_RuntimeError_on_nonzero_exit(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange
    out_dir = tmp_path / "out"

    # Act
    def _call():
        with _use_apptainer_runner(lambda *a, **kw: 7):
            isb.build_layer_from_source(
                layer="base",
                def_path=fake_def,
                pkg_root=fake_pkg_root,
                output_dir=out_dir,
            )

    # Assert
    with pytest.raises(RuntimeError, match=r"apptainer build failed.*rc=7"):
        _call()


def test_build_layer_from_source_stages_source_at_known_relative_name(
    tmp_path, fake_pkg_root, fake_def
):
    # Arrange — runner records cwd; we then check the staged tree on disk.
    out_dir = tmp_path / "out"
    seen_cwds: list[Path] = []

    def _runner(*_a, cwd, **_kw):
        seen_cwds.append(cwd)
        return 0

    # Act
    with _use_apptainer_runner(_runner):
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


# ---------------------------------------------------------------------------
# _default_apptainer_build_runner — argv shape (no real apptainer call)
# ---------------------------------------------------------------------------


def test_default_runner_sif_argv_uses_sudo_apptainer_build(tmp_path):
    # Arrange — swap subprocess.run for a real recording callable.
    seen: dict = {}

    class _FakeResult:
        returncode = 0

    def _fake_run(argv, cwd=None):
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        return _FakeResult()

    saved = isb.subprocess.run
    isb.subprocess.run = _fake_run  # type: ignore[assignment]
    try:
        # Act
        rc = isb._default_apptainer_build_runner(
            output_path=tmp_path / "x.sif",
            staged_def=tmp_path / "x.def",
            cwd=tmp_path,
            sandbox=False,
            force=True,
        )
    finally:
        isb.subprocess.run = saved  # type: ignore[assignment]
    # Assert
    assert (
        rc == 0
        and seen["argv"][:3] == ["sudo", "apptainer", "build"]
        and "--force" in seen["argv"]
        and str(tmp_path / "x.sif") in seen["argv"]
        and str(tmp_path / "x.def") in seen["argv"]
        and seen["cwd"] == str(tmp_path)
    )


def test_default_runner_sandbox_argv_uses_fakeroot_and_no_sudo(tmp_path):
    # Arrange
    seen: dict = {}

    class _FakeResult:
        returncode = 0

    def _fake_run(argv, cwd=None):
        seen["argv"] = list(argv)
        return _FakeResult()

    saved = isb.subprocess.run
    isb.subprocess.run = _fake_run  # type: ignore[assignment]
    try:
        # Act
        isb._default_apptainer_build_runner(
            output_path=tmp_path / "sb",
            staged_def=tmp_path / "x.def",
            cwd=tmp_path,
            sandbox=True,
            force=False,
        )
    finally:
        isb.subprocess.run = saved  # type: ignore[assignment]
    # Assert — sandbox path does NOT prepend sudo, DOES include --fakeroot
    assert (
        seen["argv"][0] == "apptainer"
        and "--sandbox" in seen["argv"]
        and "--fakeroot" in seen["argv"]
        and "sudo" not in seen["argv"]
        and "--force" not in seen["argv"]  # force=False
    )


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


def _build_wheel(out_dir: Path) -> Path:
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
            str(_REPO_ROOT),
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
