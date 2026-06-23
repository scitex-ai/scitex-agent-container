"""Regression tests for the marker-merge compose fix.

Before the fix, deploying the baseline CLAUDE.md into a file that already held
the ``setup_claude_md`` auto agent-section (a DIFFERENT marker style, hence
zero *deploy* markers) raised WorkspaceCLAUDEMarkerError and refused to deploy —
breaking the ``.claude/CLAUDE.md`` layout AND live agent materialization. The
merge now PRESERVES that content as a head and appends the generated section.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._to_home import _deploy_marker_protected
from scitex_agent_container.runtimes._to_home_errors import (
    WorkspaceCLAUDEMarkerError,
)
from scitex_agent_container.runtimes._to_home_text import (
    END_MARKER,
    START_MARKER_PREFIX,
    split_around_generated_section,
)

_AUTO_SECTION = (
    '<!-- agent-container:start id="x" -->\n## Agent: x\n'
    '<!-- agent-container:end id="x" -->\n'
)


def _gen(body: str = "GEN") -> str:
    return f"{START_MARKER_PREFIX} (t) -->\n{body}\n{END_MARKER}\n"


def test_split_no_deploy_markers_keeps_all_as_head() -> None:
    # Arrange — a file with the auto agent-section but NO deploy markers.
    text = _AUTO_SECTION
    # Act
    out = split_around_generated_section(text, "<t>")
    # Assert
    assert out == (text, "")


def test_split_extracts_head_before_generated_section() -> None:
    # Arrange — operator head + a generated section + tail.
    text = "HEAD\n" + _gen() + "TAIL\n"
    # Act
    head, _tail = split_around_generated_section(text, "<t>")
    # Assert
    assert head == "HEAD\n"


def test_split_extracts_tail_after_generated_section() -> None:
    # Arrange
    text = "HEAD\n" + _gen() + "TAIL\n"
    # Act
    _head, tail = split_around_generated_section(text, "<t>")
    # Assert — everything after the End marker, verbatim (the newline included).
    assert tail == "\nTAIL\n"


def test_split_malformed_duplicate_markers_fails_loud() -> None:
    # Arrange — two End markers (corruption).
    text = _gen() + END_MARKER + "\n"
    # Act
    ctx = pytest.raises(WorkspaceCLAUDEMarkerError)
    # Assert
    with ctx:
        split_around_generated_section(text, "<t>")


def test_deploy_composes_with_existing_auto_section(tmp_path) -> None:
    # Arrange — target already holds the auto agent-section (the live case);
    # source is the baseline CLAUDE.md being deployed over it.
    src = tmp_path / "CLAUDE.md"
    src.write_text("# Baseline\nsafety rules\n")
    dst = tmp_path / "out" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text(_AUTO_SECTION)
    # Act — must NOT raise; composes instead.
    _deploy_marker_protected(src, dst, config=None, rel=tmp_path)
    # Assert — the pre-existing auto-section survived alongside the new section.
    assert "## Agent: x" in dst.read_text()
