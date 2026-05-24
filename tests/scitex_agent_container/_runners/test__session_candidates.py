"""Tests for resumable-conversation listing (#192, Part B #3).

When a ``--resume`` target is gone, recovery must be INFORMATIVE +
SELECTABLE: list the conversations actually available for the agent so a
``--resume <chosen>`` is an informed choice rather than a silent fresh
start. These tests prove ``list_session_candidates`` reads the SDK's
``$HOME/.claude/projects/<encoded-cwd>/*.jsonl`` store and returns
structured candidates newest-first with a first-message snippet.

No-mocks: real on-disk ``.jsonl`` transcripts under a tmp ``$HOME``.
Conforms to STX-TQ002 (AAA markers), STX-TQ003 (descriptive names),
STX-TQ007 (one assertion per test).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scitex_agent_container._runners._session_candidates import (
    encode_claude_project,
    format_candidates,
    list_session_candidates,
)


def _write_transcript(
    home: Path,
    workdir: str,
    session_id: str,
    *,
    first_user_text: str = "do the thing",
) -> Path:
    """Create a fake SDK transcript .jsonl under the encoded projects dir."""
    proj = home / ".claude" / "projects" / encode_claude_project(workdir)
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": first_user_text}}),
        json.dumps({"type": "assistant", "message": {"content": "ok"}}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# encoding parity with Claude Code
# ---------------------------------------------------------------------------


class TestEncodeClaudeProject:
    def test_slashes_and_dots_become_dashes(self) -> None:
        # Arrange
        workdir = "/home/agent/work"
        # Act
        encoded = encode_claude_project(workdir)
        # Assert
        assert encoded == "-home-agent-work"

    def test_hidden_dir_triple_dash_collapses_to_double(self) -> None:
        # Arrange — ``/.scitex`` produces ``---scitex`` then collapses.
        workdir = "/home/agent/.scitex"
        # Act
        encoded = encode_claude_project(workdir)
        # Assert
        assert encoded == "-home-agent--scitex"


# ---------------------------------------------------------------------------
# list_session_candidates
# ---------------------------------------------------------------------------


class TestListSessionCandidates:
    def test_lists_session_id_from_jsonl_stem(self, tmp_path: Path) -> None:
        # Arrange
        home = tmp_path / "home"
        _write_transcript(home, "/work", "uuid-aaa")
        # Act
        candidates = list_session_candidates("/work", home=home)
        # Assert
        assert candidates[0].session_id == "uuid-aaa"

    def test_includes_first_user_message_snippet(self, tmp_path: Path) -> None:
        # Arrange
        home = tmp_path / "home"
        _write_transcript(home, "/work", "uuid-aaa", first_user_text="resume me please")
        # Act
        candidates = list_session_candidates("/work", home=home)
        # Assert
        assert candidates[0].first_message == "resume me please"

    def test_orders_candidates_newest_first(self, tmp_path: Path) -> None:
        # Arrange — two transcripts; bump the second's mtime to be newer.
        home = tmp_path / "home"
        _write_transcript(home, "/work", "uuid-old")
        newer = _write_transcript(home, "/work", "uuid-new")
        now = time.time()
        os.utime(newer, (now, now + 100))
        # Act
        candidates = list_session_candidates("/work", home=home)
        # Assert — newest (uuid-new) is first.
        assert candidates[0].session_id == "uuid-new"

    def test_missing_projects_dir_returns_empty_list(self, tmp_path: Path) -> None:
        # Arrange — a home with no projects dir for this workdir.
        home = tmp_path / "home"
        home.mkdir()
        # Act
        candidates = list_session_candidates("/work", home=home)
        # Assert
        assert candidates == []

    def test_limit_caps_returned_candidate_count(self, tmp_path: Path) -> None:
        # Arrange — three transcripts, limit to one.
        home = tmp_path / "home"
        for i, sid in enumerate(("uuid-a", "uuid-b", "uuid-c")):
            p = _write_transcript(home, "/work", sid)
            os.utime(p, (time.time(), time.time() + i))
        # Act
        candidates = list_session_candidates("/work", home=home, limit=1)
        # Assert
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# format_candidates
# ---------------------------------------------------------------------------


class TestFormatCandidates:
    def test_empty_list_renders_sentinel_line(self) -> None:
        # Arrange
        candidates: list = []
        # Act
        rendered = format_candidates(candidates)
        # Assert
        assert "no resumable conversations" in rendered

    def test_renders_session_id_in_listing(self, tmp_path: Path) -> None:
        # Arrange
        home = tmp_path / "home"
        _write_transcript(home, "/work", "uuid-zzz", first_user_text="hi")
        candidates = list_session_candidates("/work", home=home)
        # Act
        rendered = format_candidates(candidates)
        # Assert
        assert "uuid-zzz" in rendered
