"""WI-4 — cross-host forwarder on ``sac listen`` (handoff §4).

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4):

  Acceptance: a node on host B sends to one on host A; the event
  arrives, ACL-checked at A.

This test drives two real ``uvicorn`` instances on loopback ports
to simulate the two-host topology:

  * "host A" — owns the target. ACL is gated here.
  * "host B" — the forwarding entry point. Records the target's
    instance under ``host="host-a"`` so the resolver routes there.

A POST to host B's ``message:send`` for that target should arrive
on host A's broker.

**Identity caveat** — this PR uses the *shared fleet bearer*
assumption (Q4 option (a) in ``/work/QUESTIONS.md``): host A and
host B run with the same listen token. The forwarder passes the
incoming ``Authorization`` header through. Per-host bearer
discovery is the natural follow-on (Q4 options (b)/(c)).

No mocks (handoff §0): real SQLite + real ``uvicorn``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn

from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import record_lineage


# Both apps in the loopback test run with the same listen token —
# WI-4 Q4(b) per-host bearer registry: the forwarder pulls
# ``peer-tokens/host-a.token`` (= this value) when forwarding to
# host A. Real deployments mint independent per-host tokens.
SHARED_TOKEN = "test-token-wi4"


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Isolated state.db + runtime/registry roots."""
    # Arrange
    db = tmp_path / "state.db"
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    # WI-4 Q4(b): seed the per-host bearer registry. The forwarder
    # pulls ``peer-tokens/host-a.token`` to authenticate when
    # forwarding to host A. ``$HOME`` is already tmp_path so the
    # default ``~/.scitex/agent-container/peer-tokens/`` lands here.
    write_peer_token(peer_host="host-a", token=SHARED_TOKEN)
    try:
        yield {"db": db, "tmp": tmp_path}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin up uvicorn on a loopback port. The app's
    ``local_host`` identity is configured at ``create_app`` time
    (see :func:`scitex_agent_container._listen.server.create_app`).
    """
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
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


def _send_payload(text: str, *, from_agent: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": {"from_agent": from_agent},
        },
    }


async def _open_sse_on_local(port: int, target: str) -> tuple[asyncio.Event, asyncio.Task]:
    """Open an SSE consumer on a local sac listen. Returns
    ``(ready_event, task)`` — the task resolves when the first
    ``data:`` frame arrives.
    """
    ready = asyncio.Event()
    captured: dict = {}

    async def consume():
        async with httpx.AsyncClient(timeout=5.0) as ac:
            async with ac.stream(
                "GET",
                f"http://127.0.0.1:{port}/agents/{target}/inbox/stream",
                headers={"authorization": f"Bearer {SHARED_TOKEN}"},
            ) as sse:
                async for line in sse.aiter_lines():
                    if line.startswith(":"):
                        ready.set()
                        continue
                    if line.startswith("data:"):
                        captured["event"] = json.loads(
                            line[len("data:") :].lstrip()
                        )
                        return

    task = asyncio.create_task(consume())
    return ready, task, captured  # type: ignore[return-value]


def test_cross_host_send_forwards_to_target_host(isolated_env) -> None:
    """End-to-end: a POST to host B's ``message:send`` for a target
    pinned to host A arrives on host A's broker.
    """
    # Arrange
    db = isolated_env["db"]
    # Register the target as a live instance on host-a.
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=0, db_path=db
    )
    # Permitted-peer is registered as a child of root, so is alice;
    # they share a group and ACL allows the send.
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)

    host_a_port = _free_port()
    host_b_port = _free_port()

    # Bind the actual port for host A onto the instances row so the
    # resolver routes to the right loopback.
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )

    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            # Subscribe on host A as alice.
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=5.0) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=5.0)
                # Now stand up host B and post to it. WI-4 forwarder
                # on host B should resolve alice→host-a and forward.
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=5.0) as ac:
                        resp = await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(
                                "hi from b", from_agent="permitted-peer"
                            ),
                            headers={
                                "authorization": f"Bearer {SHARED_TOKEN}"
                            },
                        )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"forward returned {resp.status_code}: {resp.text!r}"
                    )
                await asyncio.wait_for(sub, timeout=5.0)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    event = asyncio.run(driver())
    # Assert
    assert event.get("content") == "hi from b"


def test_cross_host_forward_preserves_from_agent_metadata(isolated_env) -> None:
    """The forwarded event keeps the original ``from_agent`` so
    host A's ACL can gate on the real sender, not the forwarding
    host's identity.
    """
    # Arrange
    db = isolated_env["db"]
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=0, db_path=db
    )
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    host_a_port = _free_port()
    host_b_port = _free_port()
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )
    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=5.0) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=5.0)
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=5.0) as ac:
                        await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(
                                "x", from_agent="permitted-peer"
                            ),
                            headers={
                                "authorization": f"Bearer {SHARED_TOKEN}"
                            },
                        )
                await asyncio.wait_for(sub, timeout=5.0)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    event = asyncio.run(driver())
    # Assert
    assert event.get("from_agent") == "permitted-peer"


# ---------------------------------------------------------------------------
# WI-4 Q4(b): missing peer-token → loud 502 (handoff §0 — no silent drop).
# ---------------------------------------------------------------------------


def test_cross_host_forward_502_when_peer_token_missing(
    tmp_path: Path,
) -> None:
    """When forwarding to a host whose bearer is NOT in
    ``peer-tokens/``, the forwarder must fail loudly with 502 and a
    message naming the missing file + the ``sac host add-peer`` fix.
    """
    # Arrange — fresh tmp env, NO peer-token written for host-z.
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    db = tmp_path / "state.db"
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    state_db.record_instance_start(
        name="alice", host="host-z", a2a_port=9999, db_path=db
    )
    app_local = create_app(token=SHARED_TOKEN, local_host="host-b")

    try:
        # Act
        from starlette.testclient import TestClient

        with TestClient(app_local) as client:
            r = client.post(
                "/agents/alice/message:send",
                json=_send_payload("hi", from_agent="permitted-peer"),
                headers={"authorization": f"Bearer {SHARED_TOKEN}"},
            )
        # Assert
        assert r.status_code == 502, r.text
        body = r.json()
        assert "host-z" in body.get("error", "")
        assert "sac host add-peer" in body.get("error", "")
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
