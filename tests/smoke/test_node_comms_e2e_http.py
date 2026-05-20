"""Smoke layer: end-to-end coverage of the node-comms substrate —
**HTTP / SSE transport** pattern.

Exercises the transport + ACL stack shipped by WI-1..WI-4 of
``HANDOFF_AGENT_COMMS_2026-05-19.md`` across real processes — no
mocks. Each test boots a real ``uvicorn`` on a loopback port, talks
to it via ``httpx`` (POST + SSE) **directly** (the wire layer, one
level below the ``a2a_*`` MCP tools), reads/writes the real SQLite
``state.db``, and gates on real per-node bearer tokens.

The MCP-tool variant of these same behaviours — driving the
``a2a_send`` / ``a2a_reply`` / ``a2a_inbox`` tool surface against the
same ``sac listen`` — lives in ``test_node_comms_e2e_mcp.py``. Shared
bring-up helpers live in ``_node_comms.py``; shared fixtures
(``comms_env`` / ``disk_tmp``) live in ``conftest.py``.

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

Substrate split
===============

Today the substrate has two SSE-publish surfaces with non-overlapping
feature sets:

* ``sac listen`` (``_listen/server.py``) carries bearer auth + WI-2
  ACL but does **not** persist or honour ``Last-Event-ID``.
* ``a2a/_server.py`` (the sac-managed agent sidecar) carries WI-1
  persistence + replay but has neither bearer auth nor the WI-2 ACL
  gate.

So cases (a)–(e) drive ``sac listen`` (the only ACL surface) and case
(f) drives ``a2a/_server.py`` (the only replay surface). Unifying the
two surfaces is an open follow-on WI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._state.state_db_nodes import grant_send
from tests.smoke._node_comms import (
    _await_subscribed_and_read_one,
    _bearer,
    _free_port,
    _run_loopback,
    _send_payload,
    _set_up_four_siblings,
    _set_up_two_groups,
    _write_a2a_yaml,
)

pytestmark = pytest.mark.smoke


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
    assert resp.status_code == 403 and "cross-group" in (body.get("reason") or ""), (
        f"unexpected response: status={resp.status_code} body={body!r}"
    )


def test_cross_group_send_without_grant_does_not_reach_recipient(comms_env):
    """Recipient subscriber sees no event when the send is denied.

    A 403 at the publisher must not still fan out to the inbox bus
    (handoff §0: "denial is the policy working"). We open gamma's
    stream first, attempt alpha → gamma, then verify nothing arrives
    within a short timeout.
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
                    f"precondition: expected 403, got {resp.status_code}: {resp.text!r}"
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
    grant_send(sender="alpha", target="gamma", db_path=db, note="smoke-test grant")
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
    assert resp.status_code == 403 and "identity spoof" in (body.get("reason") or ""), (
        f"unexpected response: status={resp.status_code} body={body!r}"
    )


# ---------------------------------------------------------------------------
# Case (e) — sibling fan-out: 4 children, every sibling pair allowed
# ---------------------------------------------------------------------------


def test_sibling_fan_out_every_pair_delivers_under_default_acl(comms_env):
    """Parent + 4 children — every sibling→sibling pair allowed.

    Spins one ``sac listen`` and walks all 12 ordered
    sibling-to-sibling pairs (alpha, beta, gamma, zeta × the other
    three). For each pair: open recipient's SSE, send, expect the
    event. A single failed pair fails the test (collected as the
    offender list in the assertion message).
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
                    json=_send_payload(f"hello-{sender}-{target}", from_agent=sender),
                    headers=_bearer(tokens[sender]),
                )
            if resp.status_code != 200:
                return (
                    f"{sender}->{target} returned {resp.status_code} body={resp.text!r}"
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
# into ``sac listen`` (see the "Substrate split" note above).
# This case therefore drives the a2a server directly, with its own
# minimal echo-handler YAML.
# ---------------------------------------------------------------------------


def test_replay_on_reconnect_uses_last_event_id_to_resume_cursor(comms_env):
    """Three publishes; subscribe halfway with ``Last-Event-ID``.

    Sequence:
      1. POST events #1, #2 with no subscriber → both persist to
         ``channel_events``; ``delivered_at`` is NULL.
      2. First subscribe (no header) replays both — captures their SSE
         ``id:`` lines.
      3. Disconnect, POST event #3 with no subscriber.
      4. Re-subscribe with ``Last-Event-ID`` = id of event #2 → server
         replays exactly event #3 (and nothing earlier).
    """
    # Arrange — build the a2a app for one echo agent.
    from scitex_agent_container.a2a._server import build_app  # local import

    # ``comms_env`` is depended on for the shared, isolated state.db
    # (``channel_events`` lives there) and env-var redirection; we need
    # only the tmp dir for the YAML.
    tmp = comms_env["tmp"]
    yml = _write_a2a_yaml(tmp, "smoke-bob")
    app = build_app([yml])
    port = _free_port()
    url_send = f"http://127.0.0.1:{port}/agents/smoke-bob/message:send"
    url_stream = f"http://127.0.0.1:{port}/agents/smoke-bob/inbox/stream"

    # Act — publish, replay, reconnect-with-cursor (all need the live server).
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
