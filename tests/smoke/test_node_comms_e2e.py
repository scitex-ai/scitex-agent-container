"""Smoke layer: end-to-end coverage of the node-comms substrate.

Exercises the transport + ACL stack shipped by WI-1..WI-4 of
``HANDOFF_AGENT_COMMS_2026-05-19.md`` across real processes — no
mocks. Each test boots a real ``uvicorn`` on a loopback port,
talks to it via ``httpx`` (POST + SSE), reads/writes the real
SQLite ``state.db``, and gates on real per-node bearer tokens.

Six AAA-marked cases — one behaviour each (TQ):

* (a) Same-group send (alpha → beta, both children of parent_a).
* (b) Cross-group deny (alpha groupA → gamma groupB) — 403 + reason.
* (c) Cross-group grant unblocks (a) — alpha → gamma after
  ``grant_send`` succeeds.
* (d) Identity-spoof rejection — alpha's bearer with
  ``metadata.from_agent="beta"`` → 403 "identity spoof".
* (e) Sibling fan-out — parent + four children (alpha, beta,
  gamma, zeta); every sibling→sibling pair allowed by default.
* (f) Replay-on-reconnect — emit with no subscriber, reconnect,
  receive via ``Last-Event-ID``.

Substrate split (see ``QUESTIONS.md`` Q5)
=========================================

Today the substrate has two SSE-publish surfaces with
non-overlapping feature sets:

* ``sac listen`` (``_listen/server.py``) carries bearer auth +
  WI-2 ACL but does **not** persist or honour ``Last-Event-ID``.
* ``a2a/_server.py`` (the sac-managed agent sidecar) carries
  WI-1 persistence + replay but has neither bearer auth nor
  the WI-2 ACL gate.

So cases (a)–(e) drive ``sac listen`` (the only ACL surface) and
case (f) drives ``a2a/_server.py`` (the only replay surface).
A follow-on WI to unify the two surfaces is flagged in
``QUESTIONS.md`` Q5.

CI-safe choices
===============

* Every uvicorn loopback binds an OS-assigned port (port 0 via
  ``socket.bind`` → ``getsockname()``).
* Every test uses a ``finally``-guarded uvicorn server teardown
  + ``asyncio.Task.cancel`` for the SSE consumer.
* Every per-test temporary directory is created under
  ``/work/.pytest-tmp/smoke-node-comms/`` — disk-backed.
  pytest's stock ``tmp_path`` lands under ``/tmp`` which is a
  64 MB tmpfs in this container; ``state.db`` + WAL files would
  fill it on a busy run, so the smoke layer rolls its own.
* Every HTTP / SSE timeout is ≤ 5 s so a stuck server never
  hangs CI longer than a minute total.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    grant_send,
    mint_node_token,
    record_lineage,
)

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Disk-backed tmp dir (NOT /tmp tmpfs)
# ---------------------------------------------------------------------------

# /tmp inside this agent container is a 64 MB tmpfs that fills up under
# state.db + WAL writes. /work is the agent's writable overlay.
_DISK_BACKED_BASE = Path("/work/.pytest-tmp/smoke-node-comms")


@pytest.fixture
def disk_tmp() -> Path:
    """A fresh temp dir under ``/work`` (not ``/tmp``).

    Created per-test; removed in ``finally`` so a failure does
    not leave state.db files behind.
    """
    _DISK_BACKED_BASE.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=_DISK_BACKED_BASE))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Real state.db + env isolation (mirrors tests/_listen test_server.py's
# isolated_env / cross_host_env, but rooted at /work via disk_tmp).
# ---------------------------------------------------------------------------


@pytest.fixture
def comms_env(disk_tmp: Path):
    """Isolated state.db + HOME + registry + runtime roots.

    Touches every read-path the comms code may consult (env var,
    module-level constant) so neither code path leaks into the
    developer's real ``~/.scitex/agent-container`` tree.
    """
    db = disk_tmp / "state.db"

    saved_env = {
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
        "SCITEX_AGENT_CONTAINER_YAML_DIRS": os.environ.get(
            "SCITEX_AGENT_CONTAINER_YAML_DIRS"
        ),
    }
    saved_consts = {
        "state_db": state_db.DEFAULT_DB_PATH,
        "registry": _reg.REGISTRY_DIR,
        "session_state": _ss.DEFAULT_STATE_ROOT,
    }

    os.environ["HOME"] = str(disk_tmp)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(disk_tmp / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(disk_tmp / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = disk_tmp / "registry"
    _ss.DEFAULT_STATE_ROOT = disk_tmp / "runtime"
    state_db.init_schema(db)
    try:
        yield {"db": db, "tmp": disk_tmp}
    finally:
        state_db.DEFAULT_DB_PATH = saved_consts["state_db"]
        _reg.REGISTRY_DIR = saved_consts["registry"]
        _ss.DEFAULT_STATE_ROOT = saved_consts["session_state"]
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


# ---------------------------------------------------------------------------
# uvicorn loopback helpers (mirrors the pattern in
# tests/scitex_agent_container/_listen/test_server.py::_run_loopback).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind a loopback socket to port 0; return the OS-assigned port."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin uvicorn on ``127.0.0.1:port`` in a daemon thread.

    Teardown sets ``should_exit`` + joins; the ``finally`` fires
    even on test failure so a hung subscriber never strands the
    server.
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


# ---------------------------------------------------------------------------
# A2A JSON-RPC body shape — the same payload the SDK uses.
# ---------------------------------------------------------------------------


def _send_payload(text: str, *, from_agent: str | None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage"}
    message = {
        "message_id": "m-smoke",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    params: dict = {"message": message}
    if from_agent is not None:
        params["metadata"] = {"from_agent": from_agent}
    msg["params"] = params
    return msg


# ---------------------------------------------------------------------------
# SSE consumer helpers
# ---------------------------------------------------------------------------


async def _await_subscribed_and_read_one(
    url: str, *, headers: dict, ready: asyncio.Event, deadline_s: float = 5.0
) -> dict:
    """Open SSE, signal ``ready`` on the first comment frame, then
    return the first ``data:`` payload as JSON."""
    async with httpx.AsyncClient(timeout=deadline_s) as ac:
        async with ac.stream("GET", url, headers=headers) as sse:
            async for line in sse.aiter_lines():
                if line.startswith(":"):
                    ready.set()
                    continue
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


async def _consume_event_with_id(url: str) -> tuple[str | None, dict]:
    """Open SSE (unauthenticated — used against a2a/_server.py), return
    ``(id_line_value, parsed_data)`` for the first event."""
    seen_id: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("id:"):
                    seen_id = line[len("id:") :].strip()
                    continue
                if line.startswith("data:"):
                    return seen_id, json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


# ---------------------------------------------------------------------------
# Listen-server bring-up: lineage + tokens + uvicorn for a fixed cast.
# ---------------------------------------------------------------------------


def _set_up_two_groups(db: Path) -> dict[str, str]:
    """Group A = parent_a + {alpha, beta}. Group B = parent_b + {gamma}.

    Mints a per-node bearer for every name (so each node can
    authenticate from its own session). Returns ``{name: token}``.
    """
    record_lineage(child="alpha", parent="parent_a", db_path=db)
    record_lineage(child="beta", parent="parent_a", db_path=db)
    record_lineage(child="gamma", parent="parent_b", db_path=db)
    return {
        "host": "smoke-host-token",
        "parent_a": mint_node_token(name="parent_a", db_path=db),
        "alpha": mint_node_token(name="alpha", db_path=db),
        "beta": mint_node_token(name="beta", db_path=db),
        "parent_b": mint_node_token(name="parent_b", db_path=db),
        "gamma": mint_node_token(name="gamma", db_path=db),
    }


def _set_up_four_siblings(db: Path) -> dict[str, str]:
    """One parent + four siblings: alpha, beta, gamma, zeta (group A only).

    Distinct cast from ``_set_up_two_groups`` so case (e) does not
    conflict with case (b)'s gamma-in-group-B placement.
    """
    for child in ("alpha", "beta", "gamma", "zeta"):
        record_lineage(child=child, parent="parent_a", db_path=db)
    tokens = {"host": "smoke-host-token"}
    for name in ("parent_a", "alpha", "beta", "gamma", "zeta"):
        tokens[name] = mint_node_token(name=name, db_path=db)
    return tokens


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Case (a) — same-group send: alpha → beta (siblings under parent_a)
# ---------------------------------------------------------------------------


def test_same_group_sibling_send_delivers_to_recipient(comms_env):
    # Arrange — lineage + tokens + sac listen on a loopback port.
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    async def driver() -> dict:
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"http://127.0.0.1:{port}/agents/beta/inbox/stream",
                headers=_bearer(tokens["beta"]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.post(
                    f"http://127.0.0.1:{port}/agents/beta/message:send",
                    json=_send_payload("hello sibling", from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"intra-group POST returned {resp.status_code}: {resp.text!r}"
                )
            await asyncio.wait_for(sub, timeout=5.0)
        finally:
            if not sub.done():
                sub.cancel()
                with contextlib.suppress(BaseException):
                    await sub
        return captured.get("event", {})

    # Act
    with _run_loopback(app, port):
        event = asyncio.run(driver())
    # Assert
    assert event.get("content") == "hello sibling"


# ---------------------------------------------------------------------------
# Case (b) — cross-group deny: alpha (group A) → gamma (group B), no grant
# ---------------------------------------------------------------------------


def test_cross_group_send_without_grant_returns_403_with_reason(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    # Act
    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            resp = c.post(
                f"http://127.0.0.1:{port}/agents/gamma/message:send",
                json=_send_payload("forbidden ping", from_agent="alpha"),
                headers=_bearer(tokens["alpha"]),
            )
    # Assert (one combined assert: status + reason substring)
    body = resp.json()
    assert (
        resp.status_code == 403
        and "cross-group" in (body.get("reason") or "")
    ), f"unexpected response: status={resp.status_code} body={body!r}"


def test_cross_group_send_without_grant_does_not_reach_recipient(comms_env):
    """Recipient subscriber sees no event when the send is denied.

    A 403 at the publisher must not still fan out to the inbox bus
    (handoff §0: "denial is the policy working"). We open gamma's
    stream first, attempt alpha → gamma, then verify nothing
    arrives within a short timeout.
    """
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    async def driver() -> bool:
        ready = asyncio.Event()
        received = asyncio.Event()

        async def consume():
            async with httpx.AsyncClient(timeout=5.0) as ac:
                async with ac.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/agents/gamma/inbox/stream",
                    headers=_bearer(tokens["gamma"]),
                ) as sse:
                    async for line in sse.aiter_lines():
                        if line.startswith(":"):
                            ready.set()
                            continue
                        if line.startswith("data:"):
                            received.set()
                            return

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.post(
                    f"http://127.0.0.1:{port}/agents/gamma/message:send",
                    json=_send_payload("forbidden", from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
            if resp.status_code != 403:
                raise RuntimeError(
                    f"precondition: expected 403, got {resp.status_code}: "
                    f"{resp.text!r}"
                )
            # Wait briefly for a stray event — there should be none.
            try:
                await asyncio.wait_for(received.wait(), timeout=0.5)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            if not sub.done():
                sub.cancel()
                with contextlib.suppress(BaseException):
                    await sub

    # Act
    with _run_loopback(app, port):
        stray_arrived = asyncio.run(driver())
    # Assert
    assert stray_arrived is False


# ---------------------------------------------------------------------------
# Case (c) — cross-group grant unblocks alpha → gamma
# ---------------------------------------------------------------------------


def test_cross_group_send_after_grant_delivers_to_recipient(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    grant_send(
        sender="alpha", target="gamma", db_path=db, note="smoke-test grant"
    )
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    async def driver() -> dict:
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"http://127.0.0.1:{port}/agents/gamma/inbox/stream",
                headers=_bearer(tokens["gamma"]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.post(
                    f"http://127.0.0.1:{port}/agents/gamma/message:send",
                    json=_send_payload("granted ping", from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"granted POST returned {resp.status_code}: {resp.text!r}"
                )
            await asyncio.wait_for(sub, timeout=5.0)
        finally:
            if not sub.done():
                sub.cancel()
                with contextlib.suppress(BaseException):
                    await sub
        return captured.get("event", {})

    # Act
    with _run_loopback(app, port):
        event = asyncio.run(driver())
    # Assert
    assert event.get("content") == "granted ping"


# ---------------------------------------------------------------------------
# Case (d) — identity spoof: alpha's bearer claims to be beta
# ---------------------------------------------------------------------------


def test_identity_spoof_via_metadata_returns_403_identity_spoof(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    # Act — alpha's bearer, but ``metadata.from_agent`` claims "beta".
    # Target "beta" is alpha's true sibling so a *non-spoof* send would
    # otherwise be allowed; this isolates the spoof gate.
    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            resp = c.post(
                f"http://127.0.0.1:{port}/agents/beta/message:send",
                json=_send_payload("not really beta", from_agent="beta"),
                headers=_bearer(tokens["alpha"]),
            )
    # Assert
    body = resp.json()
    assert (
        resp.status_code == 403
        and "identity spoof" in (body.get("reason") or "")
    ), f"unexpected response: status={resp.status_code} body={body!r}"


# ---------------------------------------------------------------------------
# Case (e) — sibling fan-out: 4 children, every sibling pair allowed
# ---------------------------------------------------------------------------


def test_sibling_fan_out_every_pair_delivers_under_default_acl(comms_env):
    """Parent + 4 children — every sibling→sibling pair allowed.

    Spins one ``sac listen`` and walks all 12 ordered
    sibling-to-sibling pairs (alpha, beta, gamma, zeta × the
    other three). For each pair: open recipient's SSE, send,
    expect the event. A single failed pair fails the test
    (collected as the offender list in the assertion message).
    """
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_four_siblings(db)
    children = ("alpha", "beta", "gamma", "zeta")
    pairs = [(s, t) for s in children for t in children if s != t]
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    async def drive_one(sender: str, target: str) -> str | None:
        """Return ``None`` on success, an error string on failure."""
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"http://127.0.0.1:{port}/agents/{target}/inbox/stream",
                headers=_bearer(tokens[target]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.post(
                    f"http://127.0.0.1:{port}/agents/{target}/message:send",
                    json=_send_payload(
                        f"hello-{sender}-{target}", from_agent=sender
                    ),
                    headers=_bearer(tokens[sender]),
                )
            if resp.status_code != 200:
                return (
                    f"{sender}->{target} returned {resp.status_code} "
                    f"body={resp.text!r}"
                )
            await asyncio.wait_for(sub, timeout=5.0)
            event = captured.get("event") or {}
            if event.get("content") != f"hello-{sender}-{target}":
                return f"{sender}->{target} got unexpected event {event!r}"
            return None
        finally:
            if not sub.done():
                sub.cancel()
                with contextlib.suppress(BaseException):
                    await sub

    async def driver() -> list[str]:
        failures: list[str] = []
        for sender, target in pairs:
            err = await drive_one(sender, target)
            if err is not None:
                failures.append(err)
        return failures

    # Act
    with _run_loopback(app, port):
        failures = asyncio.run(driver())
    # Assert
    assert failures == [], f"sibling pairs failed: {failures}"


# ---------------------------------------------------------------------------
# Case (f) — replay-on-reconnect via Last-Event-ID
#
# WI-1 (durability) is wired into the ``a2a/_server.py`` surface, NOT
# into ``sac listen``. See QUESTIONS.md Q5 for the substrate split.
# This case therefore drives the a2a server directly, with its own
# minimal echo-handler YAML.
# ---------------------------------------------------------------------------


def _write_a2a_yaml(tmp: Path, name: str) -> Path:
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": name,
            "labels": {"capabilities": "echo", "role": "assistant", "team": "smoke"},
        },
        "spec": {"a2a": {"handler": "echo", "port": 8888}},
    }
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_replay_on_reconnect_uses_last_event_id_to_resume_cursor(comms_env):
    """Three publishes; subscribe halfway with ``Last-Event-ID``.

    Sequence:
      1. POST events #1, #2 with no subscriber → both persist to
         ``channel_events``; ``delivered_at`` is NULL.
      2. First subscribe (no header) replays both — captures their
         SSE ``id:`` lines.
      3. Disconnect, POST event #3 with no subscriber.
      4. Re-subscribe with ``Last-Event-ID`` = id of event #2 →
         server replays exactly event #3 (and nothing earlier).
    """
    # Arrange — build the a2a app for one echo agent.
    from scitex_agent_container.a2a._server import build_app  # local import

    # ``comms_env`` is depended on for the shared, isolated state.db
    # (``channel_events`` lives there) and env-var redirection; we
    # need only the tmp dir for the YAML.
    tmp = comms_env["tmp"]
    yml = _write_a2a_yaml(tmp, "smoke-bob")
    app = build_app([yml])
    port = _free_port()
    url_send = f"http://127.0.0.1:{port}/agents/smoke-bob/message:send"
    url_stream = f"http://127.0.0.1:{port}/agents/smoke-bob/inbox/stream"

    with _run_loopback(app, port):
        # Two pre-subscriber publishes.
        with httpx.Client(timeout=5.0) as c:
            r1 = c.post(url_send, json=_send_payload("e1", from_agent="alice"))
            r2 = c.post(url_send, json=_send_payload("e2", from_agent="alice"))
        if r1.status_code not in (200, 201, 202) or r2.status_code not in (
            200,
            201,
            202,
        ):
            raise RuntimeError(
                f"precondition publishes failed: r1={r1.status_code} "
                f"r2={r2.status_code}"
            )

        # First subscribe — replay both, capture their SSE ids.

        async def consume_two(url: str) -> list[tuple[str | None, dict]]:
            seen: list[tuple[str | None, dict]] = []
            cur_id: str | None = None
            async with httpx.AsyncClient(timeout=5.0) as ac:
                async with ac.stream("GET", url) as sse:
                    async for line in sse.aiter_lines():
                        if line.startswith("id:"):
                            cur_id = line[len("id:") :].strip()
                            continue
                        if line.startswith("data:"):
                            seen.append(
                                (
                                    cur_id,
                                    json.loads(line[len("data:") :].lstrip()),
                                )
                            )
                            if len(seen) == 2:
                                return seen
            raise AssertionError("first subscribe closed before two events")

        first = asyncio.run(consume_two(url_stream))
        cursor_id = first[-1][0]
        if cursor_id is None:
            raise RuntimeError(
                f"precondition: SSE id missing on replay frame: {first!r}"
            )

        # One post-disconnect publish.
        with httpx.Client(timeout=5.0) as c:
            r3 = c.post(url_send, json=_send_payload("e3", from_agent="alice"))
        if r3.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"precondition: third publish failed {r3.status_code}: {r3.text!r}"
            )

        # Reconnect with Last-Event-ID cursor — expect ONLY e3.
        async def consume_after_cursor(url: str, cursor: str) -> dict:
            async with httpx.AsyncClient(timeout=5.0) as ac:
                async with ac.stream(
                    "GET", url, headers={"Last-Event-ID": cursor}
                ) as sse:
                    async for line in sse.aiter_lines():
                        if line.startswith("data:"):
                            return json.loads(line[len("data:") :].lstrip())
            raise AssertionError("reconnect closed without a data frame")

        third_event = asyncio.run(consume_after_cursor(url_stream, cursor_id))

    # Assert — exactly e3 surfaces after the cursor.
    assert third_event.get("content") == "e3", (
        f"expected only e3 after Last-Event-ID={cursor_id}; got {third_event!r}"
    )
