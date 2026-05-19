"""WI-1 — channel-bus durability: end-to-end (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-1 "Durability /
replay-on-reconnect"):

  * An event POSTed to ``message:send`` while no SSE subscriber is
    connected MUST be delivered on connect (today: lost forever —
    ``_inbox_bus.py:22-24``).
  * Kill + reconnect MUST replay exactly the missed events.
  * Nothing is ever dropped silently.

These tests drive the real Starlette app via a real ``uvicorn`` on a
loopback port (no mocks, per handoff §0). The ``channel_events``
table is the durability surface; the SSE handler reads from it on
connect and stamps the SSE ``id:`` line so a Last-Event-ID reconnect
resumes at the right cursor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
import yaml
import uvicorn
from starlette.testclient import TestClient

from scitex_agent_container._state import state_db


def _write_yaml(tmpdir: Path, name: str, handler: str = "echo") -> Path:
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": name,
            "labels": {
                "capabilities": "chat,echo",
                "role": "assistant",
                "team": "scitex",
            },
        },
        "spec": {"a2a": {"handler": handler, "port": 8888}},
    }
    p = tmpdir / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """Point state.db at a tmp file for this test."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_STATE_DB", str(db))
    monkeypatch.setattr(state_db, "DEFAULT_DB_PATH", db)
    state_db.init_schema(db)
    yield db


def _send_payload(text: str, *, from_agent: str = "alice") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m-x",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": {"from_agent": from_agent},
        },
    }


# ---------------------------------------------------------------------------
# Persistence on POST — no subscriber present
# ---------------------------------------------------------------------------


def test_message_send_persists_event_to_channel_events(
    isolated_db: Path, tmp_path: Path
) -> None:
    """POST a ``message:send`` with no subscriber: the broker fans out
    to zero queues, but the event lands in ``channel_events``."""
    # Arrange
    from scitex_agent_container.a2a import build_app

    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    with TestClient(app) as client:
        # Act — publish with no subscriber.
        resp = client.post(
            "/agents/bob/message:send",
            json=_send_payload("hello durable", from_agent="alice"),
        )
    # Assert — request succeeded AND the row landed.
    assert resp.status_code in (200, 201, 202), resp.text
    with state_db.open_db(isolated_db) as conn:
        rows = conn.execute(
            "SELECT id, target, source, content, delivered_at FROM channel_events"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["target"] == "bob"
    assert rows[0]["source"] == "alice"
    assert rows[0]["content"] == "hello durable"
    assert rows[0]["delivered_at"] is None


# ---------------------------------------------------------------------------
# Reconnect replay — an event published with no subscriber is delivered
# on the first SSE connect.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin up uvicorn on a loopback port for a single test block."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Wait for "started"; raise loudly if it doesn't come up.
    import time as _time

    deadline = _time.monotonic() + 5.0
    while not server.started:
        if _time.monotonic() > deadline:
            raise RuntimeError("uvicorn loopback did not start in 5s")
        _time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        t.join(timeout=5.0)


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _consume_first_event(url: str, *, headers: dict | None = None) -> dict:
    """Open the SSE stream, read until the first ``data:`` frame, return
    the parsed event."""
    async with httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url, headers=headers or {}) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE stream {url!r} closed without a data frame")


async def _consume_event_with_id(
    url: str, *, headers: dict | None = None
) -> tuple[str | None, dict]:
    """Like :func:`_consume_first_event` but also returns the SSE ``id:``
    line that precedes the ``data:`` frame (so the caller can resume)."""
    seen_id: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url, headers=headers or {}) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("id:"):
                    seen_id = line[len("id:") :].strip()
                    continue
                if line.startswith("data:"):
                    return seen_id, json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE stream {url!r} closed without a data frame")


def test_event_posted_before_subscribe_is_replayed_on_connect(
    isolated_db: Path, tmp_path: Path
) -> None:
    """Acceptance criterion (handoff §4): "an event POSTed with no
    subscriber is delivered on connect"."""
    # Arrange
    from scitex_agent_container.a2a import build_app

    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        # Publish first, no subscriber.
        with httpx.Client(timeout=5.0) as c:
            r = c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("queued for bob"),
            )
            assert r.status_code in (200, 201, 202), r.text

        # Now subscribe and read the replay.
        event = asyncio.run(
            _consume_first_event(
                f"http://127.0.0.1:{port}/agents/bob/inbox/stream"
            )
        )
    # Assert
    assert event.get("content") == "queued for bob"


def test_replayed_event_is_marked_delivered_after_first_delivery(
    isolated_db: Path, tmp_path: Path
) -> None:
    """``delivered_at`` is set the first time the event reaches a live
    subscriber. The replay path stamps it inline so the next reconnect
    does not re-yield the same event from the undelivered window."""
    # Arrange
    from scitex_agent_container.a2a import build_app

    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("queued for bob"),
            )
        asyncio.run(
            _consume_first_event(
                f"http://127.0.0.1:{port}/agents/bob/inbox/stream"
            )
        )

    # Assert — delivered_at is non-NULL after the SSE consumed the event.
    with state_db.open_db(isolated_db) as conn:
        row = conn.execute(
            "SELECT delivered_at FROM channel_events WHERE target='bob'"
        ).fetchone()
    assert row["delivered_at"] is not None


# ---------------------------------------------------------------------------
# Last-Event-ID — reconnect resumes at the right cursor
# ---------------------------------------------------------------------------


def test_sse_id_line_is_persisted_row_id(
    isolated_db: Path, tmp_path: Path
) -> None:
    """The SSE ``id:`` line carries the channel_events row id — the
    cursor a reconnecting client echoes back as Last-Event-ID."""
    # Arrange
    from scitex_agent_container.a2a import build_app

    yml = _write_yaml(tmp_path, "bob")
    app = build_app([yml])
    port = _free_port()

    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            c.post(
                f"http://127.0.0.1:{port}/agents/bob/message:send",
                json=_send_payload("first"),
            )
        sse_id, event = asyncio.run(
            _consume_event_with_id(
                f"http://127.0.0.1:{port}/agents/bob/inbox/stream"
            )
        )

    # Assert — the SSE id matches the SQLite row id.
    assert sse_id is not None
    with state_db.open_db(isolated_db) as conn:
        row = conn.execute(
            "SELECT id FROM channel_events WHERE target='bob'"
        ).fetchone()
    assert int(sse_id) == int(row["id"])
