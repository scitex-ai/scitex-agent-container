"""Tests for editable-pointer parsing — the four shapes the tooling emits.

The shapes here are LITERAL: each ``.pth`` body is transcribed from a real
file (``/opt/venv-sac``'s ``_editable_impl_scitex_agent_container.pth``,
setuptools' compat and strict outputs, and the coverage bootstrap ``.pth``
that must NOT be mistaken for a redirect). A parser that knows only one
shape reports the other three as clean, which is how the failure class
survived twice.

Parsing is checked WITHOUT importing or exec'ing anything: these are the
files we already suspect of pointing at abandoned trees, and running them
to find out where they point is precisely the wrong instinct.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003). No monkeypatch (NM002).
"""

from __future__ import annotations

from scitex_agent_container._maintenance import _install_integrity_model as M
from scitex_agent_container._maintenance import _install_integrity_pointers as PT

# Verbatim from /opt/venv-sac (uv/hatchling shape): one bare absolute path.
_UV_PTH = "/home/u/proj/sac/.worktrees/persist/.worktrees/agent-aae4/src\n"

# setuptools "strict" mode: the path lives in the finder module it names.
_SETUPTOOLS_STRICT_PTH = (
    "import __editable___scitex_dev_0_31_0_finder; "
    "__editable___scitex_dev_0_31_0_finder.install()\n"
)

# The coverage bootstrap every venv carries. Redirects NOTHING.
_COVERAGE_PTH = (
    "import os\n"
    "if os.environ.get('COVERAGE_PROCESS_START'):\n"
    "    import coverage\n"
    "    coverage.process_startup()\n"
)

_FINDER_SOURCE = (
    "import sys\n"
    "MAPPING = {'scitex_dev': '/home/u/proj/scitex-dev/src/scitex_dev'}\n"
    "NAMESPACES = {}\n"
    "PATH_PLACEHOLDER = '__editable__.scitex_dev-0.31.0.finder'\n"
)


# ---------------------------------------------------------------------------
# Shape 1 + 4 — a bare absolute path in a .pth
# ---------------------------------------------------------------------------
def test_bare_absolute_path_yields_one_target():
    # Arrange
    text = _UV_PTH
    # Act
    parsed = PT.pth_targets(text)
    # Assert
    assert parsed == [(M.POINTER_PTH_PATH, _UV_PTH.strip())]


def test_relative_path_line_is_not_a_pointer():
    # Arrange — only an ABSOLUTE path is unambiguous evidence of a redirect.
    text = "./somewhere/relative\n"
    # Act
    parsed = PT.pth_targets(text)
    # Assert
    assert parsed == []


def test_comment_and_blank_lines_are_ignored():
    # Arrange
    text = "\n# a comment\n\n/real/target\n"
    # Act
    parsed = PT.pth_targets(text)
    # Assert
    assert parsed == [(M.POINTER_PTH_PATH, "/real/target")]


# ---------------------------------------------------------------------------
# Shape 2 — the setuptools strict .pth naming a finder module
# ---------------------------------------------------------------------------
def test_finder_import_line_names_the_finder_module():
    # Arrange
    text = _SETUPTOOLS_STRICT_PTH
    # Act
    parsed = PT.pth_targets(text)
    # Assert
    assert parsed == [(M.POINTER_PTH_IMPORT, "__editable___scitex_dev_0_31_0_finder")]


def test_coverage_bootstrap_pth_is_not_a_pointer():
    # Arrange — GREEN: an exec line naming no finder redirects nothing.
    # Counting these would bury real findings under noise every venv has.
    text = _COVERAGE_PTH
    # Act
    parsed = PT.pth_targets(text)
    # Assert
    assert parsed == []


# ---------------------------------------------------------------------------
# Shape 3 — the finder module's MAPPING dict
# ---------------------------------------------------------------------------
def test_finder_mapping_yields_the_real_directory():
    # Arrange
    source = _FINDER_SOURCE
    # Act
    targets = PT.finder_targets(source)
    # Assert
    assert targets == ["/home/u/proj/scitex-dev/src/scitex_dev"]


def test_unparsable_finder_source_yields_no_targets():
    # Arrange — a truncated finder is unreadable evidence, not a crash.
    source = "MAPPING = {'a': "
    # Act
    targets = PT.finder_targets(source)
    # Assert
    assert targets == []


def test_finder_without_mapping_yields_no_targets():
    # Arrange
    source = "import sys\nPATH_PLACEHOLDER = 'x'\n"
    # Act
    targets = PT.finder_targets(source)
    # Assert
    assert targets == []


# ---------------------------------------------------------------------------
# Attribution — which distribution does a pointer file belong to?
# ---------------------------------------------------------------------------
def test_editable_impl_filename_attributes_to_dist():
    # Arrange — the /opt/venv-sac filename.
    filename = "_editable_impl_scitex_agent_container.pth"
    # Act
    candidates = PT.candidate_dist_names(filename)
    # Assert
    assert "scitex-agent-container" in candidates


def test_dotted_editable_filename_strips_version():
    # Arrange — setuptools compat: `__editable__.<name>-<version>.pth`.
    filename = "__editable__.scitex_dev-0.31.0.pth"
    # Act
    candidates = PT.candidate_dist_names(filename)
    # Assert
    assert candidates[0] == "scitex-dev"


def test_finder_filename_strips_underscored_version():
    # Arrange — `__editable___<name>_<v>_<v>_<v>_finder.py`.
    filename = "__editable___scitex_dev_0_31_0_finder.py"
    # Act
    candidates = PT.candidate_dist_names(filename)
    # Assert
    assert "scitex-dev" in candidates


def test_candidates_are_ordered_longest_first():
    # Arrange — the caller picks the first that matches a dist it SAW, so
    # a longer, more specific name must be offered before a shorter one.
    filename = "_editable_impl_scitex_agent_container.pth"
    # Act
    candidates = PT.candidate_dist_names(filename)
    # Assert
    assert candidates == [
        "scitex-agent-container",
        "scitex-agent",
        "scitex",
    ]


def test_editable_marker_is_recognised_in_filename():
    # Arrange
    filename = "__editable__.pkg-1.0.pth"
    # Act
    recognised = PT.is_editable_pth_name(filename)
    # Assert
    assert recognised


def test_plain_pth_name_is_not_an_editable_marker():
    # Arrange
    filename = "a1_coverage.pth"
    # Act
    recognised = PT.is_editable_pth_name(filename)
    # Assert
    assert not recognised


# ---------------------------------------------------------------------------
# Existence probing is tri-state
# ---------------------------------------------------------------------------
def test_missing_target_reports_false(tmp_path):
    # Arrange
    missing = str(tmp_path / "gone")
    # Act
    exists = PT.target_exists(missing)
    # Assert
    assert exists is False


def test_present_target_reports_true(tmp_path):
    # Arrange
    present = tmp_path / "here"
    present.mkdir()
    # Act
    exists = PT.target_exists(str(present))
    # Assert
    assert exists is True
