"""End-to-end: ``POST /v1/notify`` reaches a real SSE subscriber.

This is the empirical proof for the scitex-todo escalation (P1). A
CONTAINERIZED agent's ``sac mcp channel`` SUBSCRIBES OUTBOUND to the
central ``sac listen`` daemon's a2a bus at
``GET /agents/<name>/inbox/stream`` (SSE); the daemon PUBLISHES down that
connection. The board cannot POST into the container's own a2a port
(``Connection refused``), so it POSTs the notification to the daemon's
``/v1/notify`` instead — and the daemon publishes it onto the bus the
agent is already subscribed to.

This test boots a REAL ``sac listen`` app on a REAL loopback uvicorn
socket, opens a REAL SSE subscriber on ``/agents/<name>/inbox/stream``
(exactly what a containerized agent does), POSTs to ``/v1/notify`` with
the host bearer, and asserts the subscriber receives the body. If
delivery did NOT route through the bus, the subscriber would never see
the event — so a green assertion is the end-to-end guarantee the board
needs.

No mocks (STX-NM002), mirroring the real-socket style of
``test_sac_listen_health_probe.py`` /
``test_listen_startup_sync_no_bind_block.py``:

* the server is a REAL ``uvicorn`` bound to a REAL ephemeral 127.0.0.1
  port;
* the subscriber is a REAL ``httpx`` SSE stream over that socket;
* the publish goes through the REAL ``Broker`` inside the REAL app;
* the store is REAL and isolated per test.

TQ: module docstring states intent (TQ001); AAA markers (TQ002); the
state.db / bring-up fixtures are FUNCTION scoped (TQ004) and ``yield``
their resources (TQ005); 3+-word names.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from tests.scitex_agent_container._helpers.loopback_server import (
    serve_in_thread,
    wait_until_serving,
)

HOST_TOKEN = "integration-notify-host-token"
AGENT = "containerized-worker"


# ---------------------------------------------------------------------------
# Real-socket bring-up (mirrors tests/smoke/_node_comms._run_loopback).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind a loopback socket to port 0; return the OS-assigned port."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def listen_state_db(tmp_path: Path, pg_schema: str):
    """Isolated state.db + registry/runtime dirs + channel store for the app.

    ``pg_schema`` JOINED ON 2026-08-28, AND ITS ABSENCE WAS THE FAILURE.
    ``/v1/notify`` persists the event BEFORE it publishes, and that write
    moved from the local ``state.db`` to the shared PostgreSQL (ADR-0023).
    The suite-wide guard in ``tests/_store_isolation.py`` points every test
    that does NOT ask for a real store at ``127.0.0.1:1`` — a sentinel that
    cannot connect — so this module's four tests began failing with
    ``connection to server at "127.0.0.1", port 1 failed``.

    The sentinel was never asserting "this path contacts no store"; it is
    the default isolation, and that fixture's own docstring names the
    opt-in: "Tests that need a REAL store take ``pg_schema``". This path now
    needs one, so it takes one. Adding a production fallback that shrugged
    at an unreachable store would have made the durability guarantee this
    endpoint exists to provide silently untrue.

    Function-scoped (TQ004 — no shared mutable state across tests); sets
    its env + module constants, ``yield``s the bundle (TQ005), and
    restores everything on teardown.
    """
    saved = {
        "HOME": os.environ.get("HOME"),
        "SCITEX_AGENT_CONTAINER_STATE_DB": os.environ.get(
            "SCITEX_AGENT_CONTAINER_STATE_DB"
        ),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
        ),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
        ),
    }
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_db_const = state_db.DEFAULT_DB_PATH

    db = tmp_path / "state.db"
    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield {"db": db}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.fixture
def live_listen(listen_state_db):
    """Boot the REAL ``sac listen`` app on a REAL loopback uvicorn port.

    Disables the listen lifespan's background loops (peer-sync, CI poll,
    TUI heartbeat, liveness-tick, bind-watchdog) so the test stays hermetic
    and fast; the routes + broker we exercise are unaffected. Function-
    scoped; ``yield``s the base URL; tears uvicorn down in ``finally``.
    """
    loop_disables = {
        "SAC_LISTEN_STARTUP_SYNC_DISABLED": "1",
        "SAC_PERIODIC_DRIVE_DISABLED": "1",
        "SAC_GITHUB_CI_POLLER_DISABLED": "1",
        "SAC_TUI_HEARTBEAT_DISABLED": "1",
        "SAC_LIVENESS_TICK_DISABLED": "1",
        "SAC_LISTEN_BIND_WATCHDOG_DISABLED": "1",
    }
    saved_disables = {k: os.environ.get(k) for k in loop_disables}
    os.environ.update(loop_disables)

    app = create_app(token=HOST_TOKEN, local_host="127.0.0.1")
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    # Shared startup wait: the hand-rolled 5s ceiling that used to live here
    # raced the listen lifespan (measured 7.49s under load) and turned the
    # py3.11 leg red. See tests/.../_helpers/loopback_server.py.
    thread, crash = serve_in_thread(server, port)
    wait_until_serving(server, thread, port=port, crash=crash)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        for k, v in saved_disables.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def _subscribe_then_notify(base_url: str, *, body: str, card_id: str) -> dict:
    """Subscribe to the agent's inbox SSE, POST /v1/notify, return the event.

    Models the containerized agent exactly: the SSE subscription is the
    OUTBOUND connection from the (would-be) container; the /v1/notify POST
    is the board's call from the host context. We wait for the SSE
    comment-frame (proves "subscribed") before posting so there is no race.
    """
    ready = asyncio.Event()
    captured: dict = {}

    async def consume() -> None:
        async with httpx.AsyncClient(timeout=5.0) as ac:
            async with ac.stream(
                "GET",
                f"{base_url}/agents/{AGENT}/inbox/stream",
                headers={"authorization": f"Bearer {HOST_TOKEN}"},
            ) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith(":"):
                        ready.set()
                        continue
                    if line.startswith("data:"):
                        captured["event"] = json.loads(line[len("data:") :].lstrip())
                        return

    sub = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        async with httpx.AsyncClient(timeout=5.0) as ac:
            resp = await ac.post(
                f"{base_url}/v1/notify",
                json={"agent": AGENT, "body": body, "card_id": card_id},
                headers={"authorization": f"Bearer {HOST_TOKEN}"},
            )
        captured["status"] = resp.status_code
        captured["resp_body"] = resp.json()
        await asyncio.wait_for(sub, timeout=5.0)
    finally:
        if not sub.done():
            sub.cancel()
            with contextlib.suppress(BaseException):
                await sub
    return captured


@pytest.fixture
def notify_roundtrip(live_listen):
    """Run one subscribe→/v1/notify→receive round-trip; return the capture.

    Function-scoped; the heavy real-socket round-trip runs once and the
    one-assert-per-test cases below read its result (no extra sockets).
    """
    captured = asyncio.run(
        _subscribe_then_notify(
            live_listen, body="card 7 commented by operator", card_id="card-7"
        )
    )
    yield captured


def test_notify_post_returns_200(notify_roundtrip) -> None:
    # Arrange — the round-trip fixture posted to the live /v1/notify.
    captured = notify_roundtrip
    # Act
    status = captured.get("status")
    # Assert
    assert status == 200, captured.get("resp_body")


def test_notify_reaches_the_outbound_sse_subscriber(notify_roundtrip) -> None:
    # Arrange — the subscriber modelled a containerized agent's SSE stream.
    event = notify_roundtrip.get("event", {})
    # Act
    content = event.get("content")
    # Assert — delivery routed through the bus to the real subscriber.
    assert content == "card 7 commented by operator", notify_roundtrip


def test_notify_response_reports_one_delivered_subscriber(notify_roundtrip) -> None:
    # Arrange — one SSE subscriber was connected when /v1/notify fired.
    resp_body = notify_roundtrip.get("resp_body", {})
    # Act
    count = resp_body.get("delivered_subscriber_count")
    # Assert
    assert count == 1, resp_body


def test_notify_event_carries_card_id_in_extra(notify_roundtrip) -> None:
    # Arrange — the board passed card_id so the agent can correlate.
    event = notify_roundtrip.get("event", {})
    # Act
    card_id = (event.get("extra") or {}).get("card_id")
    # Assert
    assert card_id == "card-7", event
