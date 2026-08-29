"""Tests for the Claude Code hook event ring-buffer (event_log.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.event_log import (
    DEFAULT_CAP_LINES,
    _preview_tool_input,
    append_event,
    read_recent,
    summarize,
)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "events"


# ---------------------------------------------------------------------------
# _preview_tool_input
# ---------------------------------------------------------------------------


class TestPreviewToolInput:
    """The preview helper extracts a short human-readable string per tool."""

    def test_bash_prefers_description_over_command(self):
        # Arrange
        tool = "Bash"
        payload = {"description": "run tests", "command": "pytest -q"}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "run tests"

    def test_bash_falls_back_to_command_when_no_description(self):
        # Arrange
        tool = "Bash"
        payload = {"command": "ls -la"}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "ls -la"

    def test_edit_uses_file_path_as_preview(self):
        # Arrange
        tool = "Edit"
        payload = {"file_path": "/tmp/foo.py"}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "/tmp/foo.py"

    def test_agent_prefers_description_over_prompt(self):
        # Arrange
        tool = "Agent"
        payload = {
            "description": "deep research",
            "prompt": "do a lot",
            "subagent_type": "general",
        }
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "deep research"

    def test_agent_falls_back_to_prompt_when_no_description(self):
        # Arrange
        tool = "Agent"
        payload = {"prompt": "explore"}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "explore"

    def test_mcp_tool_uses_text_field_as_preview(self):
        # Arrange
        tool = "mcp__foo__bar"
        payload = {"text": "hello world"}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == "hello world"

    def test_unknown_tool_returns_json_dump_containing_keys(self):
        # Arrange
        tool = "NewTool"
        payload = {"a": 1, "b": 2}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert "a" in out and "1" in out

    def test_empty_input_dict_returns_empty_string(self):
        # Arrange
        tool = "Read"
        payload: dict = {}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == ""

    def test_none_input_returns_empty_string_safely(self):
        # Arrange
        tool = "Read"
        payload = None
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert out == ""

    def test_preview_truncates_long_input_at_300_chars(self):
        # Arrange
        tool = "Bash"
        payload = {"command": "x" * 1_000}
        # Act
        out = _preview_tool_input(tool, payload)
        # Assert
        assert len(out) == 300


# ---------------------------------------------------------------------------
# append_event / read_recent
# ---------------------------------------------------------------------------


class TestAppendAndRead:
    """Append writes valid JSON lines readable via read_recent."""

    def test_round_trip_returns_single_event(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-a", root=tmp_root)
        # Assert
        assert len(events) == 1

    def test_round_trip_preserves_event_kind(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-a", root=tmp_root)
        # Assert
        assert events[0]["kind"] == "pretool"

    def test_round_trip_preserves_tool_name(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-a", root=tmp_root)
        # Assert
        assert events[0]["tool"] == "Read"

    def test_round_trip_captures_input_preview(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-a", root=tmp_root)
        # Assert
        assert events[0]["input_preview"] == "/tmp/x"

    def test_round_trip_includes_timestamp_field(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-a",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-a", root=tmp_root)
        # Assert
        assert "ts" in events[0]

    def test_read_recent_returns_all_appended_events(self, tmp_root: Path):
        # Arrange
        for i in range(5):
            append_event(
                "agent-b",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}},
                root=tmp_root,
            )
        # Act
        events = read_recent("agent-b", root=tmp_root)
        # Assert
        assert len(events) == 5

    def test_read_recent_returns_events_oldest_first(self, tmp_root: Path):
        # Arrange
        for i in range(5):
            append_event(
                "agent-b",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}},
                root=tmp_root,
            )
        # Act
        previews = [e["input_preview"] for e in read_recent("agent-b", root=tmp_root)]
        # Assert
        assert previews == [f"cmd{i}" for i in range(5)]

    def test_read_recent_limit_caps_count(self, tmp_root: Path):
        # Arrange
        for i in range(20):
            append_event(
                "agent-c",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"c{i}"}},
                root=tmp_root,
            )
        # Act
        events = read_recent("agent-c", limit=3, root=tmp_root)
        # Assert
        assert len(events) == 3

    def test_read_recent_limit_returns_newest_tail(self, tmp_root: Path):
        # Arrange
        for i in range(20):
            append_event(
                "agent-c",
                "pretool",
                {"tool_name": "Bash", "tool_input": {"command": f"c{i}"}},
                root=tmp_root,
            )
        # Act
        previews = [
            e["input_preview"] for e in read_recent("agent-c", limit=3, root=tmp_root)
        ]
        # Assert
        assert previews == ["c17", "c18", "c19"]

    def test_prompt_event_captures_prompt_preview(self, tmp_root: Path):
        # Arrange
        append_event("agent-d", "prompt", {"prompt": "write a function"}, root=tmp_root)
        # Act
        events = read_recent("agent-d", root=tmp_root)
        # Assert
        assert events[0]["prompt_preview"] == "write a function"

    def test_prompt_event_omits_tool_field(self, tmp_root: Path):
        # Arrange
        append_event("agent-d", "prompt", {"prompt": "write a function"}, root=tmp_root)
        # Act
        events = read_recent("agent-d", root=tmp_root)
        # Assert
        assert "tool" not in events[0]

    def test_posttool_event_captures_result_preview(self, tmp_root: Path):
        # Arrange
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
        # Act
        events = read_recent("agent-e", root=tmp_root)
        # Assert
        assert events[0]["result_preview"].startswith("hi")

    def test_run_in_background_flag_is_captured(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-f",
            "pretool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "tail -f log", "run_in_background": True},
            },
            root=tmp_root,
        )
        # Act
        events = read_recent("agent-f", root=tmp_root)
        # Assert
        assert events[0]["run_in_background"] is True

    def test_agent_name_with_unsafe_chars_is_sanitized(self, tmp_root: Path):
        # Arrange
        append_event("weird/name with spaces", "prompt", {"prompt": "x"}, root=tmp_root)
        # Act
        expected = tmp_root / "weird-name-with-spaces.jsonl"
        # Assert
        assert expected.is_file()

    def test_missing_file_returns_empty_list(self, tmp_root: Path):
        # Arrange
        agent = "never-existed"
        # Act
        events = read_recent(agent, root=tmp_root)
        # Assert
        assert events == []

    def test_invalid_json_line_is_skipped(self, tmp_root: Path):
        # Arrange
        append_event(
            "agent-g",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/a"}},
            root=tmp_root,
        )
        path = tmp_root / "agent-g.jsonl"
        with path.open("a") as f:
            f.write("not-json\n")
        append_event(
            "agent-g",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/b"}},
            root=tmp_root,
        )
        # Act
        previews = [e["input_preview"] for e in read_recent("agent-g", root=tmp_root)]
        # Assert
        assert previews == ["/a", "/b"]

    def test_failure_in_append_is_swallowed_without_raising(self, tmp_path: Path):
        # Arrange — block mkdir by making the root a regular file.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        # Act — should not raise even though mkdir would fail on a file path.
        append_event("a", "prompt", {"prompt": "x"}, root=blocker)
        # Assert — the blocker file is left untouched (no events written).
        assert blocker.read_text() == "x"


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotation:
    """Ring-buffer keeps only the last ``cap`` lines."""

    def test_rotation_caps_line_count_at_default(self, tmp_root: Path):
        # Arrange
        n = DEFAULT_CAP_LINES + 50
        for i in range(n):
            append_event(
                "agent-rot",
                "pretool",
                {"tool_name": "Read", "tool_input": {"file_path": f"/{i}"}},
                root=tmp_root,
            )
        path = tmp_root / "agent-rot.jsonl"
        # Act
        with path.open() as f:
            line_count = sum(1 for _ in f)
        # Assert
        assert line_count <= DEFAULT_CAP_LINES + 1

    def test_rotation_preserves_newest_entry(self, tmp_root: Path):
        # Arrange
        n = DEFAULT_CAP_LINES + 50
        for i in range(n):
            append_event(
                "agent-rot",
                "pretool",
                {"tool_name": "Read", "tool_input": {"file_path": f"/{i}"}},
                root=tmp_root,
            )
        # Act
        events = read_recent("agent-rot", limit=1, root=tmp_root)
        # Assert
        assert events[0]["input_preview"] == f"/{n - 1}"


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


@pytest.fixture
def last_tool_summary(tmp_root: Path):
    """Three pretools: Read, mcp__fleet__send_message, Edit."""
    append_event(
        "agent-lt",
        "pretool",
        {"tool_name": "Read", "tool_input": {"file_path": "/a"}},
        root=tmp_root,
    )
    append_event(
        "agent-lt",
        "pretool",
        {"tool_name": "mcp__fleet__send_message", "tool_input": {"text": "hi"}},
        root=tmp_root,
    )
    append_event(
        "agent-lt",
        "pretool",
        {"tool_name": "Edit", "tool_input": {"file_path": "/b"}},
        root=tmp_root,
    )
    return summarize("agent-lt", root=tmp_root)


@pytest.fixture
def populated_summary(tmp_root: Path):
    """Four pretools + one prompt event."""
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
    return summarize("agent-s", root=tmp_root)


@pytest.fixture
def pretool_and_posttool_summary(tmp_root: Path):
    """One pretool + one posttool for the same Read call."""
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
    return summarize("agent-p", root=tmp_root)


class TestSummarize:
    """summarize() pre-aggregates events for the status payload."""

    def test_empty_log_returns_canonical_empty_shape(self, tmp_root: Path):
        # Arrange
        agent = "never-seen"
        # Act
        out = summarize(agent, root=tmp_root)
        # Assert
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

    def test_last_tool_name_tracks_newest_pretool(self, last_tool_summary):
        # Arrange
        out = last_tool_summary
        # Act
        last = out["last_tool_name"]
        # Assert
        assert last == "Edit"

    def test_last_tool_at_set_when_pretool_seen(self, last_tool_summary):
        # Arrange
        out = last_tool_summary
        # Act
        last_at = out["last_tool_at"]
        # Assert
        assert last_at != ""

    def test_last_mcp_tool_name_tracks_newest_mcp_pretool(self, last_tool_summary):
        # Arrange
        out = last_tool_summary
        # Act
        last_mcp = out["last_mcp_tool_name"]
        # Assert
        assert last_mcp == "mcp__fleet__send_message"

    def test_last_mcp_tool_at_set_when_mcp_pretool_seen(self, last_tool_summary):
        # Arrange
        out = last_tool_summary
        # Act
        last_mcp_at = out["last_mcp_tool_at"]
        # Assert
        assert last_mcp_at != ""

    def test_last_tool_at_at_least_last_mcp_tool_at(self, last_tool_summary):
        # Arrange
        out = last_tool_summary
        # Act
        is_newer_or_equal = out["last_tool_at"] >= out["last_mcp_tool_at"]
        # Assert
        assert is_newer_or_equal

    def test_counts_tally_pretool_tool_names(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        counts = out["counts"]
        # Assert
        assert counts == {"Read": 2, "Agent": 1, "Bash": 1}

    def test_recent_tools_lists_every_pretool_event(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        recent = out["recent_tools"]
        # Assert
        assert len(recent) == 4

    def test_agent_calls_capture_agent_pretool_only(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        calls = out["agent_calls"]
        # Assert
        assert len(calls) == 1

    def test_agent_calls_input_preview_uses_description(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        preview = out["agent_calls"][0]["input_preview"]
        # Assert
        assert preview == "scan"

    def test_background_tasks_capture_run_in_background_pretool(
        self, populated_summary
    ):
        # Arrange
        out = populated_summary
        # Act
        tasks = out["background_tasks"]
        # Assert
        assert len(tasks) == 1

    def test_background_tasks_preview_matches_command(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        preview = out["background_tasks"][0]["input_preview"]
        # Assert
        assert preview == "sleep 1"

    def test_recent_prompts_capture_prompt_events(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        prompts = out["recent_prompts"]
        # Assert
        assert len(prompts) == 1

    def test_recent_prompts_preserve_prompt_preview(self, populated_summary):
        # Arrange
        out = populated_summary
        # Act
        preview = out["recent_prompts"][0]["prompt_preview"]
        # Assert
        assert preview == "hi"

    def test_posttool_events_do_not_increment_counts(
        self, pretool_and_posttool_summary
    ):
        # Arrange
        out = pretool_and_posttool_summary
        # Act
        counts = out["counts"]
        # Assert
        assert counts == {"Read": 1}

    def test_posttool_event_still_appears_in_recent_tools(
        self, pretool_and_posttool_summary
    ):
        # Arrange
        out = pretool_and_posttool_summary
        # Act
        kinds = [t["kind"] for t in out["recent_tools"]]
        # Assert
        assert kinds == ["pretool", "posttool"]


# ---------------------------------------------------------------------------
# Open Agent calls (LIFO matching)
# ---------------------------------------------------------------------------


@pytest.fixture
def unmatched_pretool_summary(tmp_root: Path):
    """A single Agent pretool with no matching posttool."""
    append_event(
        "ag-u",
        "pretool",
        {"tool_name": "Agent", "tool_input": {"description": "long task"}},
        root=tmp_root,
    )
    return summarize("ag-u", root=tmp_root)


@pytest.fixture
def nested_agent_summary(tmp_root: Path):
    """Two Agent pretools (outer, inner) + one posttool that pops inner."""
    for desc in ["outer", "inner"]:
        append_event(
            "ag-n",
            "pretool",
            {"tool_name": "Agent", "tool_input": {"description": desc}},
            root=tmp_root,
        )
    append_event(
        "ag-n",
        "posttool",
        {"tool_name": "Agent", "tool_response": {"content": "inner done"}},
        root=tmp_root,
    )
    return summarize("ag-n", root=tmp_root)


class TestOpenAgentCalls:
    """_compute_open_agent_calls LIFO matching detects stuck Agent calls."""

    def test_matched_pretool_posttool_pair_not_in_open_list(self, tmp_root: Path):
        # Arrange
        for kind, resp in [("pretool", None), ("posttool", {"content": "done"})]:
            payload: dict = {
                "tool_name": "Agent",
                "tool_input": {"description": "task"},
            }
            if resp:
                payload["tool_response"] = resp
            append_event("ag-m", kind, payload, root=tmp_root)
        # Act
        out = summarize("ag-m", root=tmp_root)
        # Assert
        assert out["open_agent_calls"] == []

    def test_unmatched_pretool_appears_in_open_list(self, unmatched_pretool_summary):
        # Arrange
        out = unmatched_pretool_summary
        # Act
        open_calls = out["open_agent_calls"]
        # Assert
        assert len(open_calls) == 1

    def test_unmatched_pretool_preserves_description_preview(
        self, unmatched_pretool_summary
    ):
        # Arrange
        out = unmatched_pretool_summary
        # Act
        preview = out["open_agent_calls"][0]["input_preview"]
        # Assert
        assert preview == "long task"

    def test_unmatched_pretool_reports_non_negative_age(
        self, unmatched_pretool_summary
    ):
        # Arrange
        out = unmatched_pretool_summary
        # Act
        age = out["open_agent_calls"][0]["age_seconds"]
        # Assert
        assert age is not None and age >= 0

    def test_nested_agents_lifo_leaves_one_open(self, nested_agent_summary):
        # Arrange
        out = nested_agent_summary
        # Act
        open_calls = out["open_agent_calls"]
        # Assert
        assert len(open_calls) == 1

    def test_nested_agents_lifo_keeps_outer_open(self, nested_agent_summary):
        # Arrange
        out = nested_agent_summary
        # Act
        preview = out["open_agent_calls"][0]["input_preview"]
        # Assert
        assert preview == "outer"

    def test_no_agent_events_yields_empty_open_list(self, tmp_root: Path):
        # Arrange
        append_event(
            "ag-e",
            "pretool",
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
            root=tmp_root,
        )
        # Act
        out = summarize("ag-e", root=tmp_root)
        # Assert
        assert out["open_agent_calls"] == []


# ---------------------------------------------------------------------------
# secret redaction in the on-disk previews
#
# Every preview below lands in a DURABLE per-agent JSONL. Before this, an
# agent that happened to read a credential file, or run a command that echoed
# a token, wrote that value to disk in cleartext and kept it.
#
# EVERY SECRET IN THIS BLOCK IS SYNTHETIC. Never put a real credential in a
# test: the fixture would then be the leak it exists to prevent.
# ---------------------------------------------------------------------------

# Shaped to match the third pattern in _state/_meta/secrets.py —
# (token|secret|api_key|password|bearer) followed by = or : — with a value
# distinctive enough that an assertion on its absence cannot pass by accident.
FAKE_SECRET_LINE = "declare -x GITHUB_TOKEN=ZZZsyntheticNotARealTokenZZZ"
FAKE_SECRET_VALUE = "ZZZsyntheticNotARealTokenZZZ"


class TestPreviewsRedactSecrets:
    def test_result_preview_masks_a_secret_value(self, tmp_root: Path):
        # Arrange — a posttool response carrying a secret-shaped line.
        append_event(
            "ag-redact-1",
            "posttool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "env"},
                "tool_response": {"output": FAKE_SECRET_LINE},
            },
            root=tmp_root,
        )
        # Act
        events = read_recent("ag-redact-1", root=tmp_root)
        # Assert
        assert FAKE_SECRET_VALUE not in events[-1]["result_preview"]

    def test_result_preview_still_records_something(self, tmp_root: Path):
        # Arrange — redaction must MASK, not blank the record; a preview that
        # became empty would silently destroy the diagnostic value the log
        # exists for, and would also pass the assertion above.
        append_event(
            "ag-redact-2",
            "posttool",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "env"},
                "tool_response": {"output": FAKE_SECRET_LINE},
            },
            root=tmp_root,
        )
        # Act
        preview = read_recent("ag-redact-2", root=tmp_root)[-1]["result_preview"]
        # Assert
        assert "GITHUB_TOKEN" in preview

    def test_prompt_preview_masks_a_secret_value(self, tmp_root: Path):
        # Arrange
        append_event(
            "ag-redact-3", "prompt", {"prompt": FAKE_SECRET_LINE}, root=tmp_root
        )
        # Act
        events = read_recent("ag-redact-3", root=tmp_root)
        # Assert
        assert FAKE_SECRET_VALUE not in events[-1]["prompt_preview"]

    def test_input_preview_masks_a_secret_value(self):
        # Arrange — the tool-input path has its own preview builder.
        # Act
        preview = _preview_tool_input("Bash", {"command": FAKE_SECRET_LINE})
        # Assert
        assert FAKE_SECRET_VALUE not in preview

    def test_a_benign_preview_is_left_alone(self):
        # Arrange — POSITIVE CONTROL for the assertions above: they check for
        # ABSENCE, which is also what a redactor that blanks everything, or a
        # preview builder that silently returns "", would produce. This pins
        # that ordinary text survives untouched.
        # Act
        preview = _preview_tool_input("Bash", {"command": "ls -la /tmp"})
        # Assert
        assert preview == "ls -la /tmp"
