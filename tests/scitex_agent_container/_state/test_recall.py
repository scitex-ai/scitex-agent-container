"""Tests for ``sac recall`` and the ``recall`` module.

Focus areas:
    - jsonl parsing and Entry extraction
    - duration string parsing ('8h', '30m', '1.5d', ...)
    - filter combinations (last, since, role, contains)
    - stats collection
    - ``--last`` anchored on transcript last_ts (not wallclock now)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scitex_agent_container._state.recall import (
    Entry,
    Stats,
    collect_stats,
    filter_entries,
    format_entry,
    format_stats,
    iter_entries,
    parse_duration,
)

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    @pytest.mark.parametrize(
        "spec, seconds",
        [
            ("30s", 30),
            ("5m", 300),
            ("8h", 8 * 3600),
            ("1d", 86400),
            ("2w", 2 * 604800),
            ("1.5h", 1.5 * 3600),
            ("  10m  ", 600),  # whitespace ok
            ("8H", 8 * 3600),  # case-insensitive
        ],
    )
    def test_parse_duration_returns_expected_seconds(self, spec, seconds):
        # Arrange
        input_spec = spec
        # Act
        result = parse_duration(input_spec)
        # Assert
        assert result.total_seconds() == seconds

    @pytest.mark.parametrize("bad", ["", "8", "8x", "abc", "8h30m", "-3h"])
    def test_parse_duration_rejects_invalid_input(self, bad):
        # Arrange
        bad_spec = bad
        # Act
        raised = pytest.raises(ValueError)
        # Assert
        with raised:
            parse_duration(bad_spec)


# ---------------------------------------------------------------------------
# Sample jsonl writer
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")


def _user(ts: str, text: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
        "sessionId": "sess-1",
        "cwd": "/fake/wd",
        "version": "0.0.0-test",
    }


def _assistant(
    ts: str, text: str = "", tools: list[tuple[str, dict]] | None = None
) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for name, inp in tools or []:
        content.append({"type": "tool_use", "name": name, "input": inp})
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": content},
        "sessionId": "sess-1",
    }


# ---------------------------------------------------------------------------
# iter_entries
# ---------------------------------------------------------------------------


@pytest.fixture
def infra_and_role_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            _user("2026-04-28T01:00:00Z", "first user"),
            {"type": "attachment", "timestamp": "2026-04-28T01:00:01Z"},
            _assistant("2026-04-28T01:00:02Z", "first reply"),
            {"type": "queue-operation", "timestamp": "2026-04-28T01:00:03Z"},
            _user("2026-04-28T01:00:04Z", "second user"),
        ],
    )
    return path


class TestIterEntries:
    def test_iter_entries_yields_all_records_including_infra(
        self, infra_and_role_jsonl
    ):
        # Arrange
        path = infra_and_role_jsonl
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert len(entries) == 5

    def test_iter_entries_preserves_role_message_order(self, infra_and_role_jsonl):
        # Arrange
        path = infra_and_role_jsonl
        # Act
        entries = list(iter_entries(path))
        roles_with_text = [(e.role, e.text) for e in entries if e.text]
        # Assert
        assert roles_with_text == [
            ("user", "first user"),
            ("assistant", "first reply"),
            ("user", "second user"),
        ]

    def test_extracts_tool_uses_text(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            [
                _assistant(
                    "2026-04-28T01:00:00Z",
                    "running test",
                    tools=[("Bash", {"command": "ls"}), ("Read", {"file_path": "/a"})],
                ),
            ],
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].text == "running test"

    def test_extracts_tool_uses_names(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            [
                _assistant(
                    "2026-04-28T01:00:00Z",
                    "running test",
                    tools=[("Bash", {"command": "ls"}), ("Read", {"file_path": "/a"})],
                ),
            ],
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert [name for name, _ in entries[0].tool_uses] == ["Bash", "Read"]

    def test_skips_unparseable_lines_count(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            "\n".join(
                [
                    "{not json",
                    json.dumps(_user("2026-04-28T01:00:00Z", "hi")),
                    "",
                ]
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert len(entries) == 1

    def test_skips_unparseable_lines_keeps_valid_role(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            "\n".join(
                [
                    "{not json",
                    json.dumps(_user("2026-04-28T01:00:00Z", "hi")),
                    "",
                ]
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].role == "user"


# ---------------------------------------------------------------------------
# collect_stats
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_stats_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            _user("2026-04-28T01:00:00Z", "u1"),
            _assistant("2026-04-28T01:01:00Z", "a1", tools=[("Bash", {})]),
            _assistant("2026-04-28T01:02:00Z", "", tools=[("Bash", {}), ("Read", {})]),
            {"type": "attachment", "timestamp": "2026-04-28T01:03:00Z"},
        ],
    )
    return path


class TestCollectStats:
    @pytest.mark.parametrize(
        "attr, expected",
        [
            ("total_lines", 4),
            ("parse_errors", 0),
            ("session_id", "sess-1"),
            ("cwd", "/fake/wd"),
            ("first_ts", datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc)),
            ("last_ts", datetime(2026, 4, 28, 1, 3, tzinfo=timezone.utc)),
            ("duration", timedelta(minutes=3)),
        ],
    )
    def test_collect_stats_top_level_attr(self, sample_stats_jsonl, attr, expected):
        # Arrange
        path = sample_stats_jsonl
        # Act
        stats = collect_stats(path)
        # Assert
        assert getattr(stats, attr) == expected

    @pytest.mark.parametrize(
        "type_name, count",
        [("user", 1), ("assistant", 2), ("attachment", 1)],
    )
    def test_collect_stats_by_type(self, sample_stats_jsonl, type_name, count):
        # Arrange
        path = sample_stats_jsonl
        # Act
        stats = collect_stats(path)
        # Assert
        assert stats.by_type[type_name] == count

    @pytest.mark.parametrize("tool, count", [("Bash", 2), ("Read", 1)])
    def test_collect_stats_tool_uses(self, sample_stats_jsonl, tool, count):
        # Arrange
        path = sample_stats_jsonl
        # Act
        stats = collect_stats(path)
        # Assert
        assert stats.tool_uses[tool] == count


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------


def _entry(ts_iso: str, role: str, text: str = "") -> Entry:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return Entry(ts=dt, type=role, role=role, text=text)


class TestFilterEntries:
    def test_role_filter_keeps_only_matching_role(self):
        # Arrange
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "u1"),
            _entry("2026-04-28T01:01:00Z", "assistant", "a1"),
            _entry("2026-04-28T01:02:00Z", "user", "u2"),
        ]
        # Act
        kept = list(filter_entries(entries, role="user"))
        # Assert
        assert [e.text for e in kept] == ["u1", "u2"]

    def test_contains_filter_case_insensitive(self):
        # Arrange
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "Hello world"),
            _entry("2026-04-28T01:01:00Z", "user", "goodbye"),
        ]
        # Act
        kept = list(filter_entries(entries, contains="HELLO"))
        # Assert
        assert [e.text for e in kept] == ["Hello world"]

    def test_last_anchored_on_reference_now_not_wallclock(self):
        # Arrange
        # Transcript span: 10:00 to 10:30 on a day far in the past.
        entries = [
            _entry("2020-01-01T10:00:00Z", "user", "old"),
            _entry("2020-01-01T10:15:00Z", "user", "mid"),
            _entry("2020-01-01T10:29:30Z", "user", "near-end"),
            _entry("2020-01-01T10:30:00Z", "user", "end"),
        ]
        last_15m = parse_duration("15m")
        # Act
        # Anchored on the transcript's last_ts (10:30) → keeps mid, near-end, end.
        kept = list(
            filter_entries(
                entries,
                last=last_15m,
                reference_now=datetime(2020, 1, 1, 10, 30, tzinfo=timezone.utc),
            )
        )
        # Assert
        assert [e.text for e in kept] == ["mid", "near-end", "end"]

    def test_since_until_keeps_middle_only(self):
        # Arrange
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "early"),
            _entry("2026-04-28T01:30:00Z", "user", "middle"),
            _entry("2026-04-28T02:00:00Z", "user", "late"),
        ]
        # Act
        kept = list(
            filter_entries(
                entries,
                since=datetime(2026, 4, 28, 1, 15, tzinfo=timezone.utc),
                until=datetime(2026, 4, 28, 1, 45, tzinfo=timezone.utc),
            )
        )
        # Assert
        assert [e.text for e in kept] == ["middle"]

    def test_skip_empty_default(self):
        # Arrange
        entries = [
            _entry("2026-04-28T01:00:00Z", "assistant", ""),  # no text, no tools
            _entry("2026-04-28T01:01:00Z", "user", "hi"),
        ]
        # Act
        kept = list(filter_entries(entries))
        # Assert
        assert [e.text for e in kept] == ["hi"]

    def test_no_tool_results_drops_synthetic_user_records(self):
        # Arrange
        # Mimics the jsonl shape where 'user' messages are really
        # tool_result callbacks. With --no-tool-results those should be
        # filtered out so --role user means 'human prompts only'.
        human = _entry("2026-04-28T01:00:00Z", "user", "real prompt")
        synthetic = _entry("2026-04-28T01:01:00Z", "user", "[tool_result] sent")
        synthetic.is_tool_result = True
        # Act
        kept = list(filter_entries([human, synthetic], include_tool_results=False))
        # Assert
        assert [e.text for e in kept] == ["real prompt"]

    def test_include_thinking_off_drops_thinking(self):
        # Arrange
        e = _entry("2026-04-28T01:00:00Z", "assistant", "[thinking] internal")
        # Act
        kept = list(filter_entries([e], include_thinking=False))
        # Assert
        assert kept == []

    def test_include_thinking_on_keeps(self):
        # Arrange
        e = _entry("2026-04-28T01:00:00Z", "assistant", "[thinking] internal")
        # Act
        kept = list(filter_entries([e], include_thinking=True))
        # Assert
        assert len(kept) == 1

    def test_last_auto_reference_now_uses_max_ts(self):
        # Arrange
        # No reference_now passed → derived from items.
        entries = [
            _entry("2020-01-01T10:00:00Z", "user", "old"),
            _entry("2020-01-01T10:30:00Z", "user", "end"),
        ]
        # Act
        kept = list(filter_entries(entries, last=parse_duration("15m")))
        # Assert
        assert [e.text for e in kept] == ["end"]


# ---------------------------------------------------------------------------
# _extract_text edge cases via iter_entries
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_string_content_yields_plain_text(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": "plain string"}}
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].text == "plain string"

    def test_thinking_part_is_prefixed(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "pondering"}],
                    },
                }
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].text == "[thinking] pondering"

    def test_tool_result_string_sets_is_tool_result(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "output here"}],
                    },
                }
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].is_tool_result is True

    def test_tool_result_string_includes_text(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "output here"}],
                    },
                }
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert "[tool_result] output here" in entries[0].text

    def test_tool_result_list_content_joins_parts(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [
                                    {"type": "text", "text": "alpha"},
                                    {"type": "text", "text": "beta"},
                                ],
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        text = entries[0].text
        # Assert
        assert "alpha" in text and "beta" in text

    def test_unknown_part_type_yields_empty_text(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "weird"}, "raw-non-dict"],
                    },
                }
            )
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        # No text, no tools → empty text.
        assert entries[0].text == ""

    def test_non_list_non_string_content_yields_empty_text(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": 42}})
            + "\n"
        )
        # Act
        entries = list(iter_entries(path))
        # Assert
        assert entries[0].text == ""

    def test_collect_stats_counts_parse_errors(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            "not-json\n"
            + json.dumps({"type": "user", "timestamp": "2026-04-28T01:00:00Z"})
            + "\n"
        )
        # Act
        stats = collect_stats(path)
        # Assert
        assert stats.parse_errors == 1

    def test_collect_stats_counts_total_lines_with_bad_line(self, tmp_path):
        # Arrange
        path = tmp_path / "s.jsonl"
        path.write_text(
            "not-json\n"
            + json.dumps({"type": "user", "timestamp": "2026-04-28T01:00:00Z"})
            + "\n"
        )
        # Act
        stats = collect_stats(path)
        # Assert
        assert stats.total_lines == 2


# ---------------------------------------------------------------------------
# format_stats / format_entry coverage
# ---------------------------------------------------------------------------


def _populated_stats() -> Stats:
    s = Stats()
    s.session_id = "sid-1"
    s.cwd = "/wd"
    s.version = "1.0"
    s.total_lines = 5
    s.first_ts = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    s.last_ts = s.first_ts + timedelta(minutes=42)
    s.by_type["user"] = 2
    s.tool_uses["Bash"] = 3
    return s


class TestFormatters:
    def test_format_stats_includes_session_id(self):
        # Arrange
        s = _populated_stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "sid-1" in out

    def test_format_stats_includes_tool_use_counts(self):
        # Arrange
        s = _populated_stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "Bash: 3" in out

    def test_format_stats_includes_duration(self):
        # Arrange
        s = _populated_stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "duration" in out

    def test_format_stats_empty_includes_session_id_label(self):
        # Arrange
        s = Stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "session_id" in out

    def test_format_stats_empty_omits_by_type(self):
        # Arrange
        s = Stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "by_type" not in out

    def test_format_stats_empty_omits_tool_uses(self):
        # Arrange
        s = Stats()
        # Act
        out = format_stats(s)
        # Assert
        assert "tool_uses" not in out

    def test_format_entry_truncates_long_body(self):
        # Arrange
        e = Entry(
            ts=datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc),
            type="assistant",
            role="assistant",
            text="x" * 200,
            tool_uses=[("Bash", {"command": "ls"})],
        )
        # Act
        out = format_entry(e, body_limit=50)
        # Assert
        assert "(+150 chars)" in out

    def test_format_entry_includes_tool_signature(self):
        # Arrange
        e = Entry(
            ts=datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc),
            type="assistant",
            role="assistant",
            text="x" * 200,
            tool_uses=[("Bash", {"command": "ls"})],
        )
        # Act
        out = format_entry(e, body_limit=50)
        # Assert
        assert "Bash(command)" in out

    def test_format_entry_renders_dash_for_missing_ts(self):
        # Arrange
        e = Entry(ts=None, type="user", role="user", text="")
        # Act
        out = format_entry(e)
        # Assert
        assert "[-]" in out
