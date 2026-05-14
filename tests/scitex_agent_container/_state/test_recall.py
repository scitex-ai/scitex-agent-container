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
    collect_stats,
    filter_entries,
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
    def test_basic(self, spec, seconds):
        assert parse_duration(spec).total_seconds() == seconds

    @pytest.mark.parametrize("bad", ["", "8", "8x", "abc", "8h30m", "-3h"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            parse_duration(bad)


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


class TestIterEntries:
    def test_strips_infra_and_keeps_role_messages(self, tmp_path):
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
        entries = list(iter_entries(path))
        # All 5 records yielded; role-based filtering happens in filter_entries.
        assert len(entries) == 5
        roles_with_text = [(e.role, e.text) for e in entries if e.text]
        assert roles_with_text == [
            ("user", "first user"),
            ("assistant", "first reply"),
            ("user", "second user"),
        ]

    def test_extracts_tool_uses(self, tmp_path):
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
        entries = list(iter_entries(path))
        assert len(entries) == 1
        e = entries[0]
        assert e.text == "running test"
        assert [name for name, _ in e.tool_uses] == ["Bash", "Read"]

    def test_skips_unparseable_lines(self, tmp_path):
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
        entries = list(iter_entries(path))
        # The bad line is dropped; the good one and the empty separator (skipped) → 1 entry.
        assert len(entries) == 1
        assert entries[0].role == "user"


# ---------------------------------------------------------------------------
# collect_stats
# ---------------------------------------------------------------------------


class TestCollectStats:
    def test_basic(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            [
                _user("2026-04-28T01:00:00Z", "u1"),
                _assistant("2026-04-28T01:01:00Z", "a1", tools=[("Bash", {})]),
                _assistant(
                    "2026-04-28T01:02:00Z", "", tools=[("Bash", {}), ("Read", {})]
                ),
                {"type": "attachment", "timestamp": "2026-04-28T01:03:00Z"},
            ],
        )
        s = collect_stats(path)
        assert s.total_lines == 4
        assert s.parse_errors == 0
        assert s.by_type["user"] == 1
        assert s.by_type["assistant"] == 2
        assert s.by_type["attachment"] == 1
        assert s.tool_uses["Bash"] == 2
        assert s.tool_uses["Read"] == 1
        assert s.first_ts == datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc)
        assert s.last_ts == datetime(2026, 4, 28, 1, 3, tzinfo=timezone.utc)
        assert s.duration == timedelta(minutes=3)
        assert s.session_id == "sess-1"
        assert s.cwd == "/fake/wd"


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------


def _entry(ts_iso: str, role: str, text: str = "") -> Entry:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return Entry(ts=dt, type=role, role=role, text=text)


class TestFilterEntries:
    def test_role_filter(self):
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "u1"),
            _entry("2026-04-28T01:01:00Z", "assistant", "a1"),
            _entry("2026-04-28T01:02:00Z", "user", "u2"),
        ]
        kept = list(filter_entries(entries, role="user"))
        assert [e.text for e in kept] == ["u1", "u2"]

    def test_contains_filter_case_insensitive(self):
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "Hello world"),
            _entry("2026-04-28T01:01:00Z", "user", "goodbye"),
        ]
        kept = list(filter_entries(entries, contains="HELLO"))
        assert [e.text for e in kept] == ["Hello world"]

    def test_last_anchored_on_reference_now_not_wallclock(self):
        # Transcript span: 10:00 to 10:30 on a day far in the past.
        entries = [
            _entry("2020-01-01T10:00:00Z", "user", "old"),
            _entry("2020-01-01T10:15:00Z", "user", "mid"),
            _entry("2020-01-01T10:29:30Z", "user", "near-end"),
            _entry("2020-01-01T10:30:00Z", "user", "end"),
        ]
        last_15m = parse_duration("15m")
        # Anchored on the transcript's last_ts (10:30) → keeps mid, near-end, end.
        kept = list(
            filter_entries(
                entries,
                last=last_15m,
                reference_now=datetime(2020, 1, 1, 10, 30, tzinfo=timezone.utc),
            )
        )
        assert [e.text for e in kept] == ["mid", "near-end", "end"]

    def test_since_until(self):
        entries = [
            _entry("2026-04-28T01:00:00Z", "user", "early"),
            _entry("2026-04-28T01:30:00Z", "user", "middle"),
            _entry("2026-04-28T02:00:00Z", "user", "late"),
        ]
        kept = list(
            filter_entries(
                entries,
                since=datetime(2026, 4, 28, 1, 15, tzinfo=timezone.utc),
                until=datetime(2026, 4, 28, 1, 45, tzinfo=timezone.utc),
            )
        )
        assert [e.text for e in kept] == ["middle"]

    def test_skip_empty_default(self):
        entries = [
            _entry("2026-04-28T01:00:00Z", "assistant", ""),  # no text, no tools
            _entry("2026-04-28T01:01:00Z", "user", "hi"),
        ]
        kept = list(filter_entries(entries))
        assert [e.text for e in kept] == ["hi"]

    def test_no_tool_results_drops_synthetic_user_records(self):
        # Mimics the jsonl shape where 'user' messages are really
        # tool_result callbacks. With --no-tool-results those should be
        # filtered out so --role user means 'human prompts only'.
        human = _entry("2026-04-28T01:00:00Z", "user", "real prompt")
        synthetic = _entry("2026-04-28T01:01:00Z", "user", "[tool_result] sent")
        synthetic.is_tool_result = True
        kept = list(filter_entries([human, synthetic], include_tool_results=False))
        assert [e.text for e in kept] == ["real prompt"]

    def test_include_thinking_off_drops_thinking(self):
        e = _entry("2026-04-28T01:00:00Z", "assistant", "[thinking] internal")
        kept = list(filter_entries([e], include_thinking=False))
        assert kept == []

    def test_include_thinking_on_keeps(self):
        e = _entry("2026-04-28T01:00:00Z", "assistant", "[thinking] internal")
        kept = list(filter_entries([e], include_thinking=True))
        assert len(kept) == 1

    def test_last_auto_reference_now_uses_max_ts(self):
        # No reference_now passed → derived from items.
        entries = [
            _entry("2020-01-01T10:00:00Z", "user", "old"),
            _entry("2020-01-01T10:30:00Z", "user", "end"),
        ]
        kept = list(filter_entries(entries, last=parse_duration("15m")))
        assert [e.text for e in kept] == ["end"]


# ---------------------------------------------------------------------------
# _extract_text edge cases via iter_entries
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_string_content(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": "plain string"}}
            )
            + "\n"
        )
        entries = list(iter_entries(path))
        assert entries[0].text == "plain string"

    def test_thinking_part(self, tmp_path):
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
        entries = list(iter_entries(path))
        assert entries[0].text == "[thinking] pondering"

    def test_tool_result_string(self, tmp_path):
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
        entries = list(iter_entries(path))
        e = entries[0]
        assert e.is_tool_result is True
        assert "[tool_result] output here" in e.text

    def test_tool_result_list_content(self, tmp_path):
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
        entries = list(iter_entries(path))
        assert "alpha" in entries[0].text and "beta" in entries[0].text

    def test_unknown_part_type(self, tmp_path):
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
        entries = list(iter_entries(path))
        # No text, no tools → empty text.
        assert entries[0].text == ""

    def test_non_list_non_string_content(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": 42}})
            + "\n"
        )
        entries = list(iter_entries(path))
        assert entries[0].text == ""

    def test_collect_stats_parse_error(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            "not-json\n"
            + json.dumps({"type": "user", "timestamp": "2026-04-28T01:00:00Z"})
            + "\n"
        )
        from scitex_agent_container._state.recall import collect_stats as cs

        s = cs(path)
        assert s.parse_errors == 1
        assert s.total_lines == 2


# ---------------------------------------------------------------------------
# format_stats / format_entry coverage
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_stats_includes_tools_and_duration(self):
        from scitex_agent_container._state.recall import Stats, format_stats

        s = Stats()
        s.session_id = "sid-1"
        s.cwd = "/wd"
        s.version = "1.0"
        s.total_lines = 5
        s.first_ts = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        s.last_ts = s.first_ts + timedelta(minutes=42)
        s.by_type["user"] = 2
        s.tool_uses["Bash"] = 3
        out = format_stats(s)
        assert "sid-1" in out
        assert "Bash: 3" in out
        assert "duration" in out

    def test_format_stats_empty(self):
        from scitex_agent_container._state.recall import Stats, format_stats

        s = Stats()
        out = format_stats(s)
        assert "session_id" in out
        # no by_type / no tool_uses / no time range
        assert "by_type" not in out
        assert "tool_uses" not in out

    def test_format_entry_with_truncation_and_tools(self):
        from scitex_agent_container._state.recall import Entry, format_entry

        e = Entry(
            ts=datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc),
            type="assistant",
            role="assistant",
            text="x" * 200,
            tool_uses=[("Bash", {"command": "ls"})],
        )
        out = format_entry(e, body_limit=50)
        assert "(+150 chars)" in out
        assert "Bash(command)" in out

    def test_format_entry_no_ts_no_body(self):
        from scitex_agent_container._state.recall import Entry, format_entry

        e = Entry(ts=None, type="user", role="user", text="")
        out = format_entry(e)
        assert "[-]" in out
