"""Tests for the Claude Code hook event ring-buffer (event_log.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.event_log import (
    DEFAULT_CAP_LINES,
    _preview_tool_input,
    append_event,
    read_recent,
    summarize,
)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "events"


class TestPreviewToolInput:
    """The preview helper extracts a short human-readable string per tool."""

    def test_bash_prefers_description_over_command(self):
        out = _preview_tool_input(
            "Bash",
            {"description": "run tests", "command": "pytest -q"},
        )
        assert out == "run tests"

    def test_bash_falls_back_to_command(self):
        out = _preview_tool_input("Bash", {"command": "ls -la"})
        assert out == "ls -la"

    def test_edit_uses_file_path(self):
        out = _preview_tool_input("Edit", {"file_path": "/tmp/foo.py"})
        assert out == "/tmp/foo.py"

    def test_agent_prefers_description(self):
        out = _preview_tool_input(
            "Agent",
            {
                "description": "deep research",
                "prompt": "do a lot",
                "subagent_type": "general",
            },
        )
        assert out == "deep research"

    def test_agent_falls_back_to_prompt(self):
        out = _preview_tool_input("Agent", {"prompt": "explore"})
        assert out == "explore"

    def test_mcp_tool_uses_text_field(self):
        out = _preview_tool_input("mcp__foo__bar", {"text": "hello world"})
        assert out == "hello world"

    def test_unknown_tool_returns_json_dump(self):
        out = _preview_tool_input("NewTool", {"a": 1, "b": 2})
        assert "a" in out and "1" in out

    def test_empty_input_returns_empty_string(self):
        assert _preview_tool_input("Read", {}) == ""

    def test_none_input_safe(self):
        assert _preview_tool_input("Read", None) == ""

    def test_truncates_at_300_chars(self):
        long = "x" * 1000
        out = _preview_tool_input("Bash", {"command": long})
        assert len(out) == 300


class TestAppendAndRead:
    """Append writes valid JSON lines readable via read_recent."""

    def test_round_trip_single_event(self, tmp_root: Path):
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        events = read_recent("agent-a", root=tmp_root)
        assert len(events) == 1
        assert events[0]["kind"] == "pretool"
        assert events[0]["tool"] == "Read"
        assert events[0]["input_preview"] == "/tmp/x"
        assert "ts" in events[0]

    def test_read_recent_returns_oldest_first(self, tmp_root: Path):
        for i in range(5):
            append_event(
                "agent-b",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}},
                root=tmp_root,
            )
        events = read_recent("agent-b", root=tmp_root)
        assert len(events) == 5
        previews = [e["input_preview"] for e in events]
        assert previews == [f"cmd{i}" for i in range(5)]

    def test_read_recent_limit(self, tmp_root: Path):
        for i in range(20):
            append_event(
                "agent-c",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"c{i}"}},
                root=tmp_root,
            )
        events = read_recent("agent-c", limit=3, root=tmp_root)
        assert len(events) == 3
        assert [e["input_preview"] for e in events] == ["c17", "c18", "c19"]

    def test_prompt_event_captures_prompt_preview(self, tmp_root: Path):
        append_event(
            "agent-d",
            "prompt",
            {"prompt": "write a function"},
            root=tmp_root,
        )
        events = read_recent("agent-d", root=tmp_root)
        assert events[0]["prompt_preview"] == "write a function"
        assert "tool" not in events[0]

    def test_posttool_captures_result_preview(self, tmp_root: Path):
        append_event(
            "agent-e",
            "posttool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_response": {"content": "hi\n"},
            },
            root=tmp_root,
        )
        events = read_recent("agent-e", root=tmp_root)
        assert events[0]["result_preview"].startswith("hi")

    def test_run_in_background_flag_captured(self, tmp_root: Path):
        append_event(
            "agent-f",
            "pretool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "tail -f log", "run_in_background": True},
            },
            root=tmp_root,
        )
        events = read_recent("agent-f", root=tmp_root)
        assert events[0]["run_in_background"] is True

    def test_agent_name_with_unsafe_chars_is_sanitized(self, tmp_root: Path):
        append_event(
            "weird/name with spaces",
            "prompt",
            {"prompt": "x"},
            root=tmp_root,
        )
        # Anything but [a-zA-Z0-9_.-] becomes '-'
        expected = tmp_root / "weird-name-with-spaces.jsonl"
        assert expected.is_file()

    def test_missing_file_returns_empty_list(self, tmp_root: Path):
        assert read_recent("never-existed", root=tmp_root) == []

    def test_invalid_json_line_is_skipped(self, tmp_root: Path):
        append_event(
            "agent-g",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/a"}},
            root=tmp_root,
        )
        # Corrupt the file by appending a non-JSON line.
        path = tmp_root / "agent-g.jsonl"
        with path.open("a") as f:
            f.write("not-json\n")
        append_event(
            "agent-g",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/b"}},
            root=tmp_root,
        )
        events = read_recent("agent-g", root=tmp_root)
        # The two valid entries survive; the garbage is skipped.
        assert [e["input_preview"] for e in events] == ["/a", "/b"]

    def test_failure_in_append_is_swallowed(self, tmp_path: Path):
        # Pass a root that cannot be created (regular file).
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        # Should not raise even though mkdir would fail on a file path.
        append_event("a", "prompt", {"prompt": "x"}, root=blocker)


class TestRotation:
    """Ring-buffer keeps only the last ``cap`` lines."""

    def test_rotation_caps_size(self, tmp_root: Path):
        n = DEFAULT_CAP_LINES + 50
        for i in range(n):
            append_event(
                "agent-rot",
                "pretool",
                {"tool_name": "Read", "tool_input": {"file_path": f"/{i}"}},
                root=tmp_root,
            )
        path = tmp_root / "agent-rot.jsonl"
        with path.open() as f:
            line_count = sum(1 for _ in f)
        # After rotation the buffer is at or just above the cap; it is fine
        # for a single append to land before the rotation pass runs as long
        # as the buffer stays bounded.
        assert line_count <= DEFAULT_CAP_LINES + 1
        # The newest entry survives.
        events = read_recent("agent-rot", limit=1, root=tmp_root)
        assert events[0]["input_preview"] == f"/{n - 1}"


class TestSummarize:
    """summarize() pre-aggregates events for the status payload."""

    def test_empty_log_returns_empty_shape(self, tmp_root: Path):
        out = summarize("never-seen", root=tmp_root)
        assert out == {
            "recent_tools": [],
            "recent_prompts": [],
            "agent_calls": [],
            "open_agent_calls": [],
            "background_tasks": [],
            "counts": {},
            "last_tool_at": "",
            "last_tool_name": "",
            "last_mcp_tool_at": "",
            "last_mcp_tool_name": "",
        }

    def test_last_tool_shortcuts_track_newest(self, tmp_root: Path):
        """last_tool_at/name reflect newest pretool; mcp__* updates
        last_mcp_tool_*. Non-mcp entries after an mcp entry leave the
        mcp shortcut unchanged."""
        append_event(
            "agent-lt",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/a"}},
            root=tmp_root,
        )
        append_event(
            "agent-lt",
            "pretool",
            {"tool_name": "mcp__orochi__send_message", "tool_input": {"text": "hi"}},
            root=tmp_root,
        )
        append_event(
            "agent-lt",
            "pretool",
            {"tool_name": "Edit", "tool_input": {"file_path": "/b"}},
            root=tmp_root,
        )
        out = summarize("agent-lt", root=tmp_root)
        assert out["last_tool_name"] == "Edit"
        assert out["last_tool_at"] != ""
        assert out["last_mcp_tool_name"] == "mcp__orochi__send_message"
        assert out["last_mcp_tool_at"] != ""
        # last_tool_at is >= last_mcp_tool_at (newer event wins)
        assert out["last_tool_at"] >= out["last_mcp_tool_at"]

    def test_counts_and_lists_populated(self, tmp_root: Path):
        for tool, inp in [
            ("Read", {"file_path": "/a"}),
            ("Read", {"file_path": "/b"}),
            ("Agent", {"description": "scan"}),
            ("Bash", {"command": "sleep 1", "run_in_background": True}),
        ]:
            append_event(
                "agent-s",
                "pretool",
                {"tool_name": tool, "tool_input": inp},
                root=tmp_root,
            )
        append_event("agent-s", "prompt", {"prompt": "hi"}, root=tmp_root)
        out = summarize("agent-s", root=tmp_root)
        assert out["counts"] == {"Read": 2, "Agent": 1, "Bash": 1}
        assert len(out["recent_tools"]) == 4
        assert len(out["agent_calls"]) == 1
        assert out["agent_calls"][0]["input_preview"] == "scan"
        assert len(out["background_tasks"]) == 1
        assert out["background_tasks"][0]["input_preview"] == "sleep 1"
        assert len(out["recent_prompts"]) == 1
        assert out["recent_prompts"][0]["prompt_preview"] == "hi"

    def test_only_posttool_events_not_counted(self, tmp_root: Path):
        # pretool increments counters; posttool does not, to avoid double counting
        append_event(
            "agent-p",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            root=tmp_root,
        )
        append_event(
            "agent-p",
            "posttool",
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/x"},
                "tool_response": {"content": "xxx"},
            },
            root=tmp_root,
        )
        out = summarize("agent-p", root=tmp_root)
        assert out["counts"] == {"Read": 1}
        # Both pretool and posttool appear in recent_tools
        assert len(out["recent_tools"]) == 2
        assert [t["kind"] for t in out["recent_tools"]] == ["pretool", "posttool"]


class TestOpenAgentCalls:
    """_compute_open_agent_calls LIFO matching detects stuck Agent calls."""

    def test_matched_pretool_posttool_not_open(self, tmp_root: Path):
        """Completed Agent call (pretool + posttool) does not appear in open."""
        for kind, resp in [("pretool", None), ("posttool", {"content": "done"})]:
            payload: dict = {"tool_name": "Agent", "tool_input": {"description": "task"}}
            if resp:
                payload["tool_response"] = resp
            append_event("ag-m", kind, payload, root=tmp_root)
        out = summarize("ag-m", root=tmp_root)
        assert out["open_agent_calls"] == []

    def test_unmatched_pretool_appears_open(self, tmp_root: Path):
        """Agent pretool with no posttool is in open_agent_calls."""
        append_event(
            "ag-u",
            "pretool",
            {"tool_name": "Agent", "tool_input": {"description": "long task"}},
            root=tmp_root,
        )
        out = summarize("ag-u", root=tmp_root)
        assert len(out["open_agent_calls"]) == 1
        assert out["open_agent_calls"][0]["input_preview"] == "long task"
        # age_seconds should be a small non-negative float
        assert out["open_agent_calls"][0]["age_seconds"] is not None
        assert out["open_agent_calls"][0]["age_seconds"] >= 0

    def test_nested_agents_lifo_matching(self, tmp_root: Path):
        """Two Agent pretools + one posttool leaves one open (LIFO)."""
        for desc in ["outer", "inner"]:
            append_event(
                "ag-n",
                "pretool",
                {"tool_name": "Agent", "tool_input": {"description": desc}},
                root=tmp_root,
            )
        # inner completes first
        append_event(
            "ag-n",
            "posttool",
            {"tool_name": "Agent", "tool_response": {"content": "inner done"}},
            root=tmp_root,
        )
        out = summarize("ag-n", root=tmp_root)
        assert len(out["open_agent_calls"]) == 1
        # "outer" was pushed first; "inner" pretool was popped by the posttool
        assert out["open_agent_calls"][0]["input_preview"] == "outer"

    def test_no_agent_calls_empty_open(self, tmp_root: Path):
        """No Agent events → empty open_agent_calls."""
        append_event(
            "ag-e", "pretool", {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            root=tmp_root,
        )
        out = summarize("ag-e", root=tmp_root)
        assert out["open_agent_calls"] == []
