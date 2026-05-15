"""Coverage for ``_listen._tail`` SSE streaming + heartbeat (PA-306 no-mocks).

Drives the real async generator ``_stream_tail`` with real ``tmp_path``
``session.jsonl`` fixtures and real ``asyncio`` sleeps. The HTTP layer is
exercised through :class:`starlette.testclient.TestClient`. No mocks, no
``monkeypatch`` — the heartbeat interval is a real parameter on the
generator (default 15s), tuned down for the test so the heartbeat tick is
genuine, just on a shorter schedule.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._tail import (
    _parse_iso_ts,
    _record_ts,
    _runtime_session_jsonl,
    _sse_frame,
    _stream_tail,
)
from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-tail"


# --- Helpers --------------------------------------------------------------


async def _collect(agen, limit_bytes: int = 4096, timeout: float = 5.0) -> bytes:
    """Consume ``agen`` until ``limit_bytes`` bytes are emitted or it ends."""
    buf = bytearray()

    async def _pull():
        async for chunk in agen:
            buf.extend(chunk)
            if len(buf) >= limit_bytes:
                break

    try:
        await asyncio.wait_for(_pull(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await agen.aclose()
    return bytes(buf)


# --- Pure helpers ---------------------------------------------------------


class TestParseIsoTs:
    def test_parses_valid_iso_timestamp(self):
        # Arrange
        raw = "2025-01-02T03:04:05"
        # Act
        out = _parse_iso_ts(raw)
        # Assert
        assert out == datetime(2025, 1, 2, 3, 4, 5)

    def test_strips_trailing_z_suffix(self):
        # Arrange
        raw = "2025-01-02T03:04:05Z"
        # Act
        out = _parse_iso_ts(raw)
        # Assert
        assert out == datetime(2025, 1, 2, 3, 4, 5)

    def test_invalid_string_returns_none(self):
        # Arrange
        raw = "not-a-timestamp"
        # Act
        out = _parse_iso_ts(raw)
        # Assert
        assert out is None

    def test_empty_string_returns_none(self):
        # Arrange
        raw = ""
        # Act
        out = _parse_iso_ts(raw)
        # Assert
        assert out is None

    def test_non_string_input_returns_none(self):
        # Arrange
        raw = 12345
        # Act
        out = _parse_iso_ts(raw)  # type: ignore[arg-type]
        # Assert
        assert out is None


class TestRecordTs:
    def test_reads_ts_key_first(self):
        # Arrange
        rec = {"ts": "2025-01-01T00:00:00", "timestamp": "2099-01-01T00:00:00"}
        # Act
        out = _record_ts(rec)
        # Assert
        assert out == datetime(2025, 1, 1, 0, 0, 0)

    def test_falls_back_to_timestamp_key(self):
        # Arrange
        rec = {"timestamp": "2025-06-07T08:09:10"}
        # Act
        out = _record_ts(rec)
        # Assert
        assert out == datetime(2025, 6, 7, 8, 9, 10)

    def test_non_string_ts_returns_none(self):
        # Arrange
        rec = {"ts": 12345}
        # Act
        out = _record_ts(rec)
        # Assert
        assert out is None

    def test_missing_keys_returns_none(self):
        # Arrange
        rec = {"msg": "hi"}
        # Act
        out = _record_ts(rec)
        # Assert
        assert out is None


class TestSseFrame:
    def test_event_prefix_emitted_when_named(self):
        # Arrange
        ev, data = "ping", "ok"
        # Act
        out = _sse_frame(ev, data)
        # Assert
        assert out == b"event: ping\ndata: ok\n\n"

    def test_no_event_prefix_when_none(self):
        # Arrange
        ev, data = None, "raw"
        # Act
        out = _sse_frame(ev, data)
        # Assert
        assert out == b"data: raw\n\n"


@pytest.fixture
def home_at_tmp(tmp_path):
    """Real env mutation: point HOME at tmp_path, restore on teardown."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


class TestRuntimeSessionPath:
    def test_path_includes_agent_name(self, home_at_tmp):
        # Arrange — HOME already redirected to home_at_tmp by the fixture
        # Act
        p = _runtime_session_jsonl("alice")
        # Assert
        assert (
            p
            == home_at_tmp
            / ".scitex"
            / "agent-container"
            / "runtime"
            / "alice"
            / "session.jsonl"
        )


# --- _stream_tail async generator ----------------------------------------


@pytest.mark.asyncio
class TestStreamTailNoFollow:
    async def test_missing_file_no_follow_yields_nothing(self, tmp_path):
        # Arrange
        path = tmp_path / "absent.jsonl"
        # Act
        out = await _collect(_stream_tail(path, since=None, follow=False))
        # Assert
        assert out == b""

    async def test_streams_existing_records(self, tmp_path):
        # Arrange
        path = tmp_path / "session.jsonl"
        path.write_text(
            '{"ts": "2025-01-01T00:00:00", "msg": "hello"}\n'
            '{"ts": "2025-01-01T00:00:01", "msg": "world"}\n',
            encoding="utf-8",
        )
        # Act
        out = await _collect(_stream_tail(path, since=None, follow=False))
        # Assert
        assert b"hello" in out and b"world" in out

    async def test_line_numbers_increase_monotonically(self, tmp_path):
        # Arrange
        path = tmp_path / "session.jsonl"
        path.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        # Act
        out = await _collect(_stream_tail(path, since=None, follow=False))
        # Assert
        assert b'"line_no": 1' in out and b'"line_no": 2' in out

    async def test_empty_line_is_skipped(self, tmp_path):
        # Arrange — blank line between two real records
        path = tmp_path / "session.jsonl"
        path.write_text('{"a":1}\n\n{"a":2}\n', encoding="utf-8")
        # Act
        out = await _collect(_stream_tail(path, since=None, follow=False))
        # Assert — only two payloads emitted (line_no 1 and 3, line 2 skipped)
        assert out.count(b'"line_no"') == 2

    async def test_invalid_json_falls_back_to_raw(self, tmp_path):
        # Arrange
        path = tmp_path / "session.jsonl"
        path.write_text("not-json-at-all\n", encoding="utf-8")
        # Act
        out = await _collect(_stream_tail(path, since=None, follow=False))
        # Assert
        assert b'"raw": "not-json-at-all"' in out


@pytest.mark.asyncio
class TestStreamTailSinceFilter:
    async def test_record_before_since_is_dropped(self, tmp_path):
        # Arrange
        path = tmp_path / "session.jsonl"
        path.write_text(
            '{"ts": "2020-01-01T00:00:00", "msg": "old"}\n'
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n',
            encoding="utf-8",
        )
        since = datetime(2025, 1, 1)
        # Act
        out = await _collect(_stream_tail(path, since=since, follow=False))
        # Assert
        assert b"old" not in out

    async def test_record_after_since_is_emitted(self, tmp_path):
        # Arrange
        path = tmp_path / "session.jsonl"
        path.write_text(
            '{"ts": "2020-01-01T00:00:00", "msg": "old"}\n'
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n',
            encoding="utf-8",
        )
        since = datetime(2025, 1, 1)
        # Act
        out = await _collect(_stream_tail(path, since=since, follow=False))
        # Assert
        assert b"future" in out

    async def test_record_without_ts_before_seen_is_dropped(self, tmp_path):
        # Arrange — no-ts record arrives before any in-range record
        path = tmp_path / "session.jsonl"
        path.write_text(
            '{"msg": "no-ts-leading"}\n'
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n',
            encoding="utf-8",
        )
        since = datetime(2025, 1, 1)
        # Act
        out = await _collect(_stream_tail(path, since=since, follow=False))
        # Assert
        assert b"no-ts-leading" not in out

    async def test_record_without_ts_after_seen_passes_through(self, tmp_path):
        # Arrange — no-ts record arrives after the boundary has been crossed
        path = tmp_path / "session.jsonl"
        path.write_text(
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n'
            '{"msg": "trailing-no-ts"}\n',
            encoding="utf-8",
        )
        since = datetime(2025, 1, 1)
        # Act
        out = await _collect(_stream_tail(path, since=since, follow=False))
        # Assert
        assert b"trailing-no-ts" in out


@pytest.mark.asyncio
class TestStreamTailFollow:
    async def test_follow_waits_for_file_to_appear(self, tmp_path):
        # Arrange — file doesn't exist when generator starts
        path = tmp_path / "later.jsonl"

        async def _delayed_create():
            await asyncio.sleep(0.2)
            path.write_text('{"msg": "arrived"}\n', encoding="utf-8")

        # Act — kick off generator and creator concurrently, collect briefly
        creator = asyncio.create_task(_delayed_create())
        out = await _collect(
            _stream_tail(path, since=None, follow=True, poll_interval=0.05),
            timeout=2.0,
        )
        await creator
        # Assert
        assert b"arrived" in out

    async def test_follow_emits_heartbeat_when_idle(self, tmp_path):
        # Arrange — empty file, short heartbeat interval (real ticks)
        path = tmp_path / "idle.jsonl"
        path.write_text("", encoding="utf-8")
        # Act — wait long enough for at least one heartbeat
        out = await _collect(
            _stream_tail(
                path,
                since=None,
                follow=True,
                heartbeat_interval=0.1,
                poll_interval=0.02,
            ),
            limit_bytes=64,
            timeout=2.0,
        )
        # Assert
        assert b": keep-alive" in out

    async def test_follow_picks_up_appended_records(self, tmp_path):
        # Arrange
        path = tmp_path / "growing.jsonl"
        path.write_text('{"msg": "first"}\n', encoding="utf-8")

        async def _append_later():
            await asyncio.sleep(0.15)
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"msg": "appended"}\n')

        # Act
        appender = asyncio.create_task(_append_later())
        out = await _collect(
            _stream_tail(path, since=None, follow=True, poll_interval=0.05),
            timeout=2.0,
        )
        await appender
        # Assert
        assert b"appended" in out

    async def test_follow_aclose_terminates_idle_generator(self, tmp_path):
        # Arrange — empty file; generator will park in the poll/sleep loop
        path = tmp_path / "park.jsonl"
        path.write_text("", encoding="utf-8")
        agen = _stream_tail(
            path,
            since=None,
            follow=True,
            heartbeat_interval=10.0,
            poll_interval=0.05,
        )

        async def _drain():
            async for _ in agen:
                return  # never reached during idle

        pull_task = asyncio.create_task(_drain())
        # Let the generator reach its inner ``await asyncio.sleep``
        await asyncio.sleep(0.15)
        # Act — cancelling the consumer must close the generator cleanly,
        # exercising the GeneratorExit/CancelledError branch in _tail.py
        pull_task.cancel()
        # Assert — cancellation propagates through the generator's except clause
        with pytest.raises(asyncio.CancelledError):
            await pull_task


# --- HTTP layer via TestClient -------------------------------------------


@pytest.fixture
def isolated_home(tmp_path):
    """Real env mutation across HOME + registry + runtime dirs."""
    keys = {
        "HOME": str(tmp_path),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": str(tmp_path / "registry"),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def http_client(isolated_home):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


def _write_session(home: Path, name: str, body: str) -> Path:
    rt = home / ".scitex" / "agent-container" / "runtime" / name
    rt.mkdir(parents=True, exist_ok=True)
    p = rt / "session.jsonl"
    p.write_text(body, encoding="utf-8")
    return p


class TestTailHttpEndpoint:
    def test_invalid_since_value_is_treated_as_none(self, http_client, isolated_home):
        # Arrange — unparseable since must not crash; all records pass
        _write_session(isolated_home, "badsince", '{"msg": "kept"}\n')
        # Act
        resp = http_client.get(
            "/agents/badsince/tail?follow=false&since=garbage",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        # Assert
        assert "kept" in resp.text

    def test_payload_records_are_valid_json(self, http_client, isolated_home):
        # Arrange
        _write_session(
            isolated_home, "jsoncheck", '{"ts": "2025-01-01T00:00:00", "k": "v"}\n'
        )
        # Act
        resp = http_client.get(
            "/agents/jsoncheck/tail?follow=false",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        data_line = next(ln for ln in resp.text.splitlines() if ln.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        # Assert
        assert payload["record"]["k"] == "v"
