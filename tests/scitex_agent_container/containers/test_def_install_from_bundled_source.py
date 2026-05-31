"""Contract test: shipped .def files install sac from bundled source.

Locks the invariant established in this PR: the in-SIF sac must always
equal the source tree that shipped the .def, regardless of how/where the
package was installed (editable, wheel, SIF). The .def files achieve
this by:

  1. Declaring a ``%files`` entry that copies the package source into the
     image at ``/opt/scitex-agent-container-src``.
  2. Installing sac from that absolute path in ``%post``
     (``uv pip install /opt/scitex-agent-container-src``).
  3. NOT referencing ``git+https://...`` for the sac install — that
     would silently re-pin the image to whatever happened to be on the
     branch at build time.

Any future edit that drops one of these has to update this test too.
"""

from __future__ import annotations

import pytest

# The CLI helper that knows where the recipes live + the relative source
# name the .def files must reference. Single source of truth: if the
# helper renames _STAGED_SRC_NAME, the .def files must match.
from scitex_agent_container.cli_pkg._image_source_build import _STAGED_SRC_NAME
from scitex_agent_container.cli_pkg.image_group import _LAYERS, _RECIPES_DIR

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
    # Arrange — the .def declares the %files entry the staging helper depends on.
    expected = f"{_STAGED_SRC_NAME} /opt/scitex-agent-container-src"
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
    # Arrange — %post installs from the bundled source path — not from git.
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
    # Arrange — banned substrings; any of these drifts from the source tree.
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
        if f"\n    {_STAGED_SRC_NAME} " not in (_RECIPES_DIR / name).read_text()
    ]
    # Assert
    assert missing == [], (
        f"{missing} must declare '{_STAGED_SRC_NAME}' as a %files source "
        f"so the staging helper's copy is what gets bundled."
    )
