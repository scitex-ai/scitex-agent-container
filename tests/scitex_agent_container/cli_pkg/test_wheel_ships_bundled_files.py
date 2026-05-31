"""Test that the built wheel ships the bundled files the SIF build needs.

The source-bundled SIF build (cli_pkg/_image_source_build.py) needs
pyproject.toml + README.md alongside the package at SIF-build time. Both
are placed inside the wheel under ``scitex_agent_container/_bundled/``
via ``[tool.hatch.build.targets.wheel.force-include]`` in pyproject.toml.

This test actually builds the wheel and inspects its contents — if a
future edit to pyproject.toml drops the force-include directive, the
wheel-install path of :func:`locate_bundled_pyproject` would silently
fall through to the editable-fallback (which won't help operators who
installed sac from a wheel).

Skipped if ``hatchling`` isn't importable (which means the repo's build
toolchain isn't available — common in minimal test images).
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# Locate the repo root from the package install. In editable mode
# ``Path(scitex_agent_container.__file__).resolve()`` points at
# ``<repo>/src/scitex_agent_container/__init__.py``, so:
#   parents[0] -> scitex_agent_container/
#   parents[1] -> src/
#   parents[2] -> <repo>/
import scitex_agent_container

_PKG_PATH = Path(scitex_agent_container.__file__).resolve()
_REPO_ROOT = _PKG_PATH.parents[2]
# Hatchling needs pyproject.toml at the repo root.
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


_HAVE_PIP = True
try:
    import pip  # noqa: F401
except ImportError:
    _HAVE_PIP = False

pytestmark = [
    pytest.mark.skipif(
        not _PYPROJECT.is_file(),
        reason="wheel-build smoke requires editable install with repo root on disk",
    ),
    pytest.mark.skipif(
        not _HAVE_PIP,
        reason="wheel-build smoke requires pip; CI runners have it, minimal "
        "container images may not",
    ),
]


def _build_wheel(out_dir: Path) -> Path:
    """Build the wheel + return the .whl path. Uses pip wheel (always available).

    ``pip wheel --no-deps`` runs the PEP 517 backend (hatchling) to
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
