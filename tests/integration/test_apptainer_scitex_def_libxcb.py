"""Static contract: ``apptainer-scitex.def`` must apt-install libxcb1 + libgl1
+ libglib2.0-0.

Why these specific packages live in the :scitex layer's %post apt-get:

* ``libxcb1``  — ``import scitex`` pulls in ``scitex_cv`` which imports
  ``cv2`` which dlopens ``libxcb.so.1`` (cv2's headless GUI shim still
  links against xcb even with no display). Missing → ``ImportError:
  libxcb.so.1: cannot open shared object file``. This parked every
  ripple-wm compute lane (ripple / gpfa / nt / memory_load) until the
  2026-06-08 SIF rebuild.
* ``libgl1``   — pairs with libxcb to cover cv2's OpenGL probe; without
  it cv2 crashes inside ``cv2.imread`` on Spartan compute nodes that
  lack the mesa-dev runtime.
* ``libglib2.0-0`` — already in the :base layer (lib-gthread silent-
  fallback fix, 2026-06-06) but kept here belt-and-suspenders so a
  future :base trim cannot silently regress the :scitex layer.

This test pins the requirement as code: drop a package from the .def
and CI yells before the SIF rebuild lands a regression.

TQ002 markers (AAA) per repo convention; one assertion per test (TQ007).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Resolve the .def relative to this test file so the test works regardless
# of the worktree path. tests/integration/ is 2 levels below the repo
# root; .def is at src/scitex_agent_container/containers/apptainer-scitex.def.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCITEX_DEF = (
    _REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "containers"
    / "apptainer-scitex.def"
)


@pytest.fixture(scope="module")
def scitex_def_text() -> str:
    # Arrange
    assert _SCITEX_DEF.exists(), f"apptainer-scitex.def missing at {_SCITEX_DEF}"
    return _SCITEX_DEF.read_text()


def _apt_install_block(text: str) -> str:
    """Return the first ``apt-get install ...`` chunk (joined lines)."""
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("apt-get install"):
            in_block = True
        if in_block:
            out.append(stripped.rstrip("\\").strip())
            if not stripped.endswith("\\"):
                break
    return " ".join(out)


def test_apt_install_block_lists_libxcb1(scitex_def_text: str) -> None:
    # Arrange
    block = _apt_install_block(scitex_def_text)
    # Act
    present = "libxcb1" in block.split()
    # Assert
    assert present, (
        f"libxcb1 missing from apt-get install in apptainer-scitex.def:\n{block}"
    )


def test_apt_install_block_lists_libgl1(scitex_def_text: str) -> None:
    # Arrange
    block = _apt_install_block(scitex_def_text)
    # Act
    present = "libgl1" in block.split()
    # Assert
    assert present, (
        f"libgl1 missing from apt-get install in apptainer-scitex.def:\n{block}"
    )


def test_apt_install_block_lists_libglib2_0_0(scitex_def_text: str) -> None:
    # Arrange
    block = _apt_install_block(scitex_def_text)
    # Act
    present = "libglib2.0-0" in block.split()
    # Assert
    assert present, (
        f"libglib2.0-0 missing from apt-get install in apptainer-scitex.def:\n{block}"
    )
