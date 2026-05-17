"""Tests for ``_mcp/_tools/_subagent.py``.

Real filesystem under ``tmp_path`` simulates ``~/.claude/projects/``.
No monkeypatching of stdlib; ``projects_root`` is passed in via the
function's explicit kwarg (STX-NM002).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._mcp._tools import _subagent

# ─── fixtures ────────────────────────────────────────────────────────────────


PROJECT_PATH = "/tmp/fake-proj-for-tests"
PROJECT_HASH = "-tmp-fake-proj-for-tests"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def _make_layout(tmp_path: Path) -> Path:
    """Build a ``~/.claude/projects/<hash>/<session>/subagents/`` layout
    under tmp_path and return the simulated projects-root."""
    root = tmp_path / "claude-projects"
    (root / PROJECT_HASH / SESSION_A / "subagents").mkdir(parents=True)
    (root / PROJECT_HASH / SESSION_B / "subagents").mkdir(parents=True)
    return root


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _user_record(text: str, ts: str = "2026-05-15T12:00:00.000Z") -> dict:
    return {
        "type": "user",
        "agentId": "abc123",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "sessionId": SESSION_A,
    }


def _assistant_record(tool: str | None, ts: str = "2026-05-15T12:00:01.000Z") -> dict:
    content: list[dict] = []
    if tool is not None:
        content.append({"type": "tool_use", "name": tool, "input": {}})
    else:
        content.append({"type": "text", "text": "ok"})
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
        "timestamp": ts,
        "sessionId": SESSION_A,
    }


# ─── subagent_get_state — happy paths ────────────────────────────────────────


def test_get_state_missing_project_dir_returns_empty_list(tmp_path):
    # Arrange — projects_root exists but no project hash dir inside.
    root = tmp_path / "claude-projects"
    root.mkdir()
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out == []


def test_get_state_missing_projects_root_returns_empty_list(tmp_path):
    # Arrange
    missing = tmp_path / "no-such-root"
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=missing)
    # Assert
    assert out == []


def test_get_state_no_subagents_returns_empty_list(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out == []


def test_get_state_returns_one_entry_per_jsonl(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    sub_a = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa111.jsonl"
    sub_b = root / PROJECT_HASH / SESSION_B / "subagents" / "agent-bbb222.jsonl"
    _write_jsonl(sub_a, [_user_record("hello"), _assistant_record("Read")])
    _write_jsonl(sub_b, [_user_record("world"), _assistant_record("Write")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert {r["id"] for r in out} == {"aaa111", "bbb222"}


def test_get_state_session_filter_restricts_to_one_session(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    _write_jsonl(
        root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl",
        [_user_record("a")],
    )
    _write_jsonl(
        root / PROJECT_HASH / SESSION_B / "subagents" / "agent-bbb.jsonl",
        [_user_record("b")],
    )
    # Act
    out = _subagent.subagent_get_state(
        project_path=PROJECT_PATH,
        session_id=SESSION_A,
        projects_root=root,
    )
    # Assert
    assert [r["id"] for r in out] == ["aaa"]


def test_get_state_agent_id_filter_returns_only_match(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    base = root / PROJECT_HASH / SESSION_A / "subagents"
    _write_jsonl(base / "agent-aaa.jsonl", [_user_record("a")])
    _write_jsonl(base / "agent-bbb.jsonl", [_user_record("b")])
    # Act
    out = _subagent.subagent_get_state(
        agent_id="aaa", project_path=PROJECT_PATH, projects_root=root
    )
    # Assert
    assert [r["id"] for r in out] == ["aaa"]


def test_get_state_agent_id_filter_missing_returns_empty(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    base = root / PROJECT_HASH / SESSION_A / "subagents"
    _write_jsonl(base / "agent-aaa.jsonl", [_user_record("a")])
    # Act
    out = _subagent.subagent_get_state(
        agent_id="nonexistent", project_path=PROJECT_PATH, projects_root=root
    )
    # Assert
    assert out == []


def test_get_state_extracts_description_from_first_user_message(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("Implement feature X")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["description"] == "Implement feature X"


def test_get_state_extracts_last_tool_use_name(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(
        p,
        [
            _user_record("go"),
            _assistant_record("Read", ts="2026-05-15T12:00:01Z"),
            _assistant_record("Bash", ts="2026-05-15T12:00:02Z"),
        ],
    )
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_tool"] == "Bash"


def test_get_state_last_tool_none_when_no_tool_use(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("go"), _assistant_record(None)])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_tool"] is None


def test_get_state_records_last_user_ts(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(
        p,
        [
            _user_record("go", ts="2026-05-15T12:00:00Z"),
            _assistant_record("Read", ts="2026-05-15T12:00:01Z"),
            _user_record("more", ts="2026-05-15T12:00:02Z"),
        ],
    )
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_user_ts_iso"] == "2026-05-15T12:00:02Z"


def test_get_state_records_last_assistant_ts(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(
        p,
        [
            _user_record("go", ts="2026-05-15T12:00:00Z"),
            _assistant_record("Read", ts="2026-05-15T12:00:01Z"),
            _user_record("more", ts="2026-05-15T12:00:02Z"),
        ],
    )
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_assistant_ts_iso"] == "2026-05-15T12:00:01Z"


def test_get_state_records_size_bytes(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("x")])
    expected_size = p.stat().st_size
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["size_bytes"] == expected_size


def test_get_state_records_mtime_iso_suffix(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("x")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["mtime_iso"].endswith("Z")


def test_get_state_records_mtime_epoch_matches_filesystem(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("x")])
    expected_mtime = p.stat().st_mtime
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["mtime_epoch"] == expected_mtime


def test_get_state_records_project_hash(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("x")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["project_hash"] == PROJECT_HASH


def test_get_state_records_session_id(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-aaa.jsonl"
    _write_jsonl(p, [_user_record("x")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["session_id"] == SESSION_A


def test_get_state_reports_completed_marker_when_present(tmp_path):
    # Arrange — embed a task-notification + completed payload in the tail.
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-done.jsonl"
    completed_rec = {
        "type": "user",
        "message": {
            "role": "user",
            "content": "task-notification status=completed all-green",
        },
        "timestamp": "2026-05-15T12:05:00Z",
    }
    _write_jsonl(p, [_user_record("go"), _assistant_record("Read"), completed_rec])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["has_completed_marker"] is True


def test_get_state_completed_marker_absent_by_default(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-live.jsonl"
    _write_jsonl(p, [_user_record("go"), _assistant_record("Read")])
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["has_completed_marker"] is False


# ─── subagent_get_state — robustness ─────────────────────────────────────────


def test_get_state_malformed_lines_do_not_raise(tmp_path):
    # Arrange — mix valid + malformed lines.
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-bad.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        fh.write("not valid json\n")
        fh.write(json.dumps(_user_record("real")) + "\n")
        fh.write("{also-broken\n")
    # Act — must not raise.
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert len(out) == 1


def test_get_state_malformed_lines_recover_valid_description(tmp_path):
    # Arrange — mix valid + malformed lines.
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-bad.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        fh.write("not valid json\n")
        fh.write(json.dumps(_user_record("real")) + "\n")
        fh.write("{also-broken\n")
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["description"] == "real"


def test_get_state_empty_jsonl_still_emits_entry(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-empty.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert len(out) == 1


def test_get_state_empty_jsonl_description_is_none(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-empty.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["description"] is None


def test_get_state_empty_jsonl_last_tool_is_none(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-empty.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_tool"] is None


def test_get_state_empty_jsonl_size_is_zero(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-empty.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["size_bytes"] == 0


def _write_huge_transcript(p: Path) -> None:
    """Helper for tail-only contract tests — writes a >_TAIL_BYTES
    transcript whose head + tail contain probe records we can find."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_user_record("first")) + "\n")
        pad = {"type": "noise", "blob": "x" * 1024}
        for _ in range(200):
            fh.write(json.dumps(pad) + "\n")
        fh.write(
            json.dumps(_assistant_record("Glob", ts="2026-05-15T13:00:00Z")) + "\n"
        )


def test_get_state_huge_transcript_recovers_last_tool_from_tail(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-big.jsonl"
    _write_huge_transcript(p)
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["last_tool"] == "Glob"


def test_get_state_huge_transcript_recovers_description_from_head(tmp_path):
    # Arrange
    root = _make_layout(tmp_path)
    p = root / PROJECT_HASH / SESSION_A / "subagents" / "agent-big.jsonl"
    _write_huge_transcript(p)
    # Act
    out = _subagent.subagent_get_state(project_path=PROJECT_PATH, projects_root=root)
    # Assert
    assert out[0]["description"] == "first"


# ─── MCP registration ────────────────────────────────────────────────────────


class _FakeMCP:
    def __init__(self) -> None:
        self.registered: list = []

    def tool(self, *args, **kw):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


def test_register_subagent_tools_registers_get_state_only():
    # Arrange — classification is orochi's job, not sac's; MCP surface
    # exposes pure state only.
    mcp = _FakeMCP()
    # Act
    _subagent.register_subagent_tools(mcp)
    # Assert
    assert {fn.__name__ for fn in mcp.registered} == {"subagent_get_state"}


# ─── project hash translation ────────────────────────────────────────────────


def test_project_hash_translates_slashes_to_dashes():
    # Arrange
    path = "/home/ywatanabe/proj/lead"
    # Act
    h = _subagent._project_hash_for(path)
    # Assert
    assert h == "-home-ywatanabe-proj-lead"
