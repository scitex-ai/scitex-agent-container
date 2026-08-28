"""THE reconnect test: drop mid-stream, resume, lose nothing, repeat nothing.

Everything else in this directory tests a primitive. This tests the PROMISE
those primitives exist to keep, end to end, through a real ASGI app on a real
socket: an SSE subscriber that dies holding ``Last-Event-ID: N`` comes back
and receives exactly the events after N — no skips, no replays — including
the ones published while it was gone.

That promise is what the 2026-08-28 move to PostgreSQL (ADR-0023) put at
risk in three separate ways, and this file checks all three at once:

* ids are PER-TARGET now, so ``mark_delivered`` must be told which target it
  is marking or it silently marks another agent's rows;
* the id comes from a counter ROW rather than a sequence, so commit order and
  id order agree and a reader using ``id > cursor`` cannot step past an event
  that has not landed yet;
* every database call inside the SSE generator is an ``asyncio.to_thread``
  hop, so a stream that awaits one still behaves like a stream.

WHY A REAL UVICORN AND NOT ``httpx.ASGITransport``
==================================================
Measured here, httpx 0.28.1: ``ASGITransport.handle_async_request`` does
``await self.app(scope, receive, send)`` and only THEN builds the response
stream from the collected body parts. It cannot stream an endless body, so
pointing it at an SSE endpoint hangs until the test times out — which is
exactly what the first version of this file did, three tests, 25s each.
``run_loopback`` serves the same app on a real loopback port, which is
strictly stronger anyway: closing the stream is a real TCP close, so
``request.is_disconnected()`` observes the drop the way it does in
production instead of never firing.

NO MOCKS. The app below is thin, but nothing in it is a stand-in: the
publish path calls the real :func:`mint_event` and the real
:func:`persist_event`, the stream route calls the real
:func:`a2a._inbox_stream.inbox_stream`, and the broker is the real
:class:`Broker`. Only the ROUTING is local — the production sidecar app
would drag in the A2A SDK dispatcher, which has nothing to do with
durability and would make a durability failure look like an SDK failure.
``_Ctx`` is the two attributes that handler reads, which is data, not a
double.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from scitex_agent_container._state.state_db_channel import persist_event
from scitex_agent_container._state.state_db_channel_store import (
    reset_channel_connection,
)
from scitex_agent_container.a2a._inbox_bus import Broker, mint_event
from scitex_agent_container.a2a._inbox_stream import inbox_stream
from tests.scitex_agent_container._helpers.loopback_server import run_loopback
from tests.scitex_agent_container._helpers.ports import reserved_port

AGENT = "lead"


@pytest.fixture(autouse=True)
def _drop_cached_connection():
    """Close the process-wide handle around every test in this module."""
    reset_channel_connection()
    yield
    reset_channel_connection()


@dataclass
class _Ctx:
    """The two attributes ``inbox_stream`` reads off its context.

    ``yamls`` is the known-agent set (the handler 404s on an unknown name)
    and ``inbox`` is the live broker. Both are the real values the sidecar
    would hold; this is a container for them, not a substitute for one.
    """

    inbox: Broker = field(default_factory=Broker)
    yamls: dict[str, Any] = field(default_factory=lambda: {AGENT: {}})


def _build_app(ctx: _Ctx) -> Starlette:
    """A real ASGI app over the real publish and stream code paths."""

    async def post_message(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        event = mint_event(name, content=body["content"], from_agent="alice")
        # Durability first, exactly as every production publisher does it:
        # persist, stamp the minted id onto the envelope, then publish.
        row_id = await asyncio.to_thread(persist_event, target=name, event=event)
        event["_row_id"] = row_id
        await ctx.inbox.publish(name, event)
        return JSONResponse({"id": row_id, "msg_id": event["msg_id"]})

    async def get_stream(request: Request) -> Response:
        return await inbox_stream(request, ctx)

    return Starlette(
        routes=[
            Route("/agents/{name}/message:send", post_message, methods=["POST"]),
            Route("/agents/{name}/inbox/stream", get_stream, methods=["GET"]),
        ]
    )


@contextlib.contextmanager
def _client() -> Iterator[httpx.Client]:
    """A real HTTP client against a real uvicorn serving the real handlers."""
    app = _build_app(_Ctx())
    with reserved_port() as sock:
        with run_loopback(app, sock=sock) as port:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=15.0
            ) as client:
                yield client


def _post(client: httpx.Client, content: str) -> int:
    response = client.post(
        f"/agents/{AGENT}/message:send", json={"content": content}
    )
    return int(response.json()["id"])


def _read_frames(
    response: httpx.Response, *, want: int
) -> list[tuple[int, dict[str, Any]]]:
    """Collect ``want`` ``(id, event)`` pairs off a live SSE response.

    Comment frames (the ``: sac-channel ready`` hello and any keepalive) are
    skipped — they carry no id and no data, which is exactly what makes them
    safe for a client to ignore.
    """
    frames: list[tuple[int, dict[str, Any]]] = []
    pending_id: int | None = None
    for line in response.iter_lines():
        if line.startswith("id:"):
            pending_id = int(line[3:].strip())
        elif line.startswith("data:"):
            frames.append((pending_id or 0, json.loads(line[5:].strip())))
            pending_id = None
            if len(frames) >= want:
                return frames
    return frames


def test_reconnect_resumes_at_the_next_event_with_no_gap(pg_schema: str) -> None:
    """Read 3 of 5, drop, publish 2 more, resume from the 3rd id.

    The stream must then yield events 4, 5, 6 and 7 — in that order, once
    each. A skip loses a message silently; a replay double-delivers one. Both
    are invisible to the client, which is why this asserts the exact sequence
    rather than a count.
    """
    # Arrange
    with _client() as client:
        for n in range(1, 6):
            _post(client, f"m{n}")
        with client.stream("GET", f"/agents/{AGENT}/inbox/stream") as first_open:
            seen = _read_frames(first_open, want=3)
        cursor = seen[-1][0]
        for n in range(6, 8):
            _post(client, f"m{n}")
        # Act
        with client.stream(
            "GET",
            f"/agents/{AGENT}/inbox/stream",
            headers={"Last-Event-ID": str(cursor)},
        ) as second_open:
            resumed = _read_frames(second_open, want=4)
    # Assert
    assert [(row_id, event["content"]) for row_id, event in resumed] == [
        (4, "m4"),
        (5, "m5"),
        (6, "m6"),
        (7, "m7"),
    ]


def test_resumed_stream_starts_one_past_the_captured_cursor(pg_schema: str) -> None:
    """The first id after a resume is exactly ``Last-Event-ID + 1``.

    Stated separately from the sequence above because it is the property a
    client actually relies on: the cursor it echoes back is the last frame it
    PROCESSED, so re-receiving it would be a duplicate and skipping past it
    would be a loss.
    """
    # Arrange
    with _client() as client:
        for n in range(1, 6):
            _post(client, f"m{n}")
        with client.stream("GET", f"/agents/{AGENT}/inbox/stream") as first_open:
            seen = _read_frames(first_open, want=3)
        cursor = seen[-1][0]
        _post(client, "m6")
        # Act
        with client.stream(
            "GET",
            f"/agents/{AGENT}/inbox/stream",
            headers={"Last-Event-ID": str(cursor)},
        ) as second_open:
            resumed = _read_frames(second_open, want=1)
    # Assert
    assert resumed[0][0] == cursor + 1


def test_fresh_subscriber_receives_events_published_while_absent(
    pg_schema: str,
) -> None:
    """The original WI-1 acceptance: no ``Last-Event-ID``, nothing lost.

    A client with no cursor gets every UNDELIVERED row. This is the case a
    container that has just started is in, and it is the one an age-based
    retention sweep must never touch (ADR-0023 §6).
    """
    # Arrange
    with _client() as client:
        for n in range(1, 4):
            _post(client, f"m{n}")
        # Act
        with client.stream("GET", f"/agents/{AGENT}/inbox/stream") as opened:
            replayed = _read_frames(opened, want=3)
    # Assert
    assert [event["content"] for _, event in replayed] == ["m1", "m2", "m3"]
