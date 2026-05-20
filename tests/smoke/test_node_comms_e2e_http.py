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

AAA-marked cases — one behaviour each (TQ):

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
* (g) Fresh-replay on the ``sac listen`` surface — a single
  pre-subscriber publish surfaces on first connect.
* (h.1) Listen-surface replay order — two pre-subscriber publishes
  arrive in id order on first connect.
* (h.2) Listen-surface cursor resume — ``Last-Event-ID`` =
  id(previous) yields only the post-cursor publish.
* (i) Listen-surface denied send leaves ``channel_events`` empty —
  denial is the policy working (handoff §0).

Substrate split
===============

Historically the substrate had two SSE-publish surfaces with
non-overlapping feature sets — ``sac listen`` carried bearer-auth +
WI-2 ACL but no persistence, while ``a2a/_server.py`` carried
WI-1 persistence + replay but no auth/ACL. The follow-on WI-1 finish-
work has now unified them: ``_listen/server.py``'s
``node_message_send`` persists every accepted publish to
``channel_events`` and ``node_inbox_stream`` replays missed events on
connect (honouring ``Last-Event-ID``). Cases (a)–(e) still drive
``sac listen`` for the ACL semantics; case (f) drives the
``a2a/_server.py`` surface for replay; new cases (g)–(i) drive WI-1
durability through ``sac listen`` so the same acceptance criteria
hold there.
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


# ---------------------------------------------------------------------------
# Case (g) — WI-1 finish-work: an event POSTed to ``sac listen`` with no
# subscriber is delivered on the NEXT subscribe (fresh-replay path —
# undelivered rows in ``channel_events``).
#
# This is the WI-1 acceptance "an event POSTed with no subscriber is
# delivered on connect" applied to the ``_listen/server.py`` surface
# (the WI-3 external-node substrate). Until the durability wiring landed
# there, this publish was silently dropped — the exact failure mode the
# handoff §0 hard rules forbid.
# ---------------------------------------------------------------------------


def test_listen_publish_with_no_subscriber_is_replayed_on_next_connect(comms_env):
    """Publish first, subscribe second — the event must arrive.

    Sequence:
      1. POST alpha → beta with **no SSE subscriber attached** (the
         ``Broker.publish`` fan-out returns ``delivered=0``).
      2. Open beta's inbox stream — the WI-1 fresh-replay path yields
         the missed event from ``channel_events`` (``delivered_at IS
         NULL``).

    Acceptance (handoff §4): "an event POSTed with no subscriber is
    delivered on connect; … nothing is ever dropped silently."
    """
    # Arrange — siblings under one parent so default ACL allows the send.
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    async def driver() -> dict:
        # Pre-subscriber publish.
        async with httpx.AsyncClient(timeout=5.0) as ac:
            resp = await ac.post(
                f"http://127.0.0.1:{port}/agents/beta/message:send",
                json=_send_payload("delayed delivery", from_agent="alpha"),
                headers=_bearer(tokens["alpha"]),
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"precondition publish failed: {resp.status_code}: {resp.text!r}"
            )

        # Now subscribe — the WI-1 fresh-replay yields the missed event.
        ready = asyncio.Event()
        captured: dict = {}

        async def consume() -> None:
            captured["event"] = await _await_subscribed_and_read_one(
                f"http://127.0.0.1:{port}/agents/beta/inbox/stream",
                headers=_bearer(tokens["beta"]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
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
    assert event.get("content") == "delayed delivery", (
        f"WI-1 fresh-replay on _listen surface did not surface the missed "
        f"pre-subscriber publish; got {event!r}"
    )


# ---------------------------------------------------------------------------
# Case (h) — WI-1 finish-work: kill + reconnect with ``Last-Event-ID``
# resumes the stream at the next undelivered row on the ``sac listen``
# surface. Mirrors case (f) but against ``_listen/server.py`` rather
# than ``a2a/_server.py``.
#
# Split into two TQ007-clean tests, one behaviour each:
#
#   (h.1) ``test_listen_replay_on_reconnect_replays_pre_subscribe_events_in_id_order``
#         — pre-subscriber publishes are replayed in insertion (id)
#         order on first connect. Validates the *replay* half of the
#         durability contract.
#
#   (h.2) ``test_listen_replay_on_reconnect_resumes_only_post_cursor_event_with_last_event_id``
#         — after disconnect + a fresh publish, reconnecting with
#         ``Last-Event-ID`` = the previously-seen id yields **only**
#         the post-cursor event. Validates the *cursor resume* half.
# ---------------------------------------------------------------------------


def _consume_sse_n(
    url: str,
    n: int,
    *,
    bearer: dict[str, str],
    last_event_id: str | None = None,
) -> "asyncio.coroutines.Coroutine[None, None, list[tuple[str | None, dict]]]":
    """Open an SSE stream and return the first ``n`` ``data:`` events
    paired with the most recent ``id:`` line that preceded each.

    Lifted to a module helper so cases (h.1) and (h.2) share one
    implementation without coupling their assertions.
    """

    async def _run() -> list[tuple[str | None, dict]]:
        seen: list[tuple[str | None, dict]] = []
        cur_id: str | None = None
        headers = dict(bearer)
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        async with httpx.AsyncClient(timeout=5.0) as ac:
            async with ac.stream("GET", url, headers=headers) as sse:
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
                        if len(seen) == n:
                            return seen
        raise AssertionError(
            f"SSE {url!r} closed before {n} events (saw {len(seen)})"
        )

    return _run()


def test_listen_replay_on_reconnect_replays_pre_subscribe_events_in_id_order(comms_env):
    """Two pre-subscriber publishes are replayed in id order on first connect.

    Isolates the *replay-order* half of case (h). The cursor-resume
    half lives in the sister test below.
    """
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    url_send = f"http://127.0.0.1:{port}/agents/beta/message:send"
    url_stream = f"http://127.0.0.1:{port}/agents/beta/inbox/stream"

    async def driver() -> list[str]:
        # Two pre-subscriber publishes.
        async with httpx.AsyncClient(timeout=5.0) as ac:
            for i in (1, 2):
                r = await ac.post(
                    url_send,
                    json=_send_payload(f"e{i}", from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"precondition publish e{i} failed {r.status_code}: {r.text!r}"
                    )
        # First subscribe — replays e1 + e2; surface their contents in
        # arrival order so the assertion can compare on order alone.
        frames = await _consume_sse_n(
            url_stream, 2, bearer=_bearer(tokens["beta"])
        )
        return [evt.get("content") for _id, evt in frames]

    # Act
    with _run_loopback(app, port):
        contents = asyncio.run(driver())
    # Assert
    assert contents == ["e1", "e2"], f"replay order wrong: {contents!r}"


def test_listen_replay_on_reconnect_resumes_only_post_cursor_event_with_last_event_id(
    comms_env,
):
    """Three publishes; subscribe + disconnect after #2, publish #3,
    re-subscribe with ``Last-Event-ID = id(#2)`` → only #3 arrives.
    """
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    url_send = f"http://127.0.0.1:{port}/agents/beta/message:send"
    url_stream = f"http://127.0.0.1:{port}/agents/beta/inbox/stream"

    async def driver() -> dict:
        # Two pre-subscriber publishes.
        async with httpx.AsyncClient(timeout=5.0) as ac:
            for i in (1, 2):
                r = await ac.post(
                    url_send,
                    json=_send_payload(f"e{i}", from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"precondition publish e{i} failed {r.status_code}: {r.text!r}"
                    )
        # First subscribe — replays e1 + e2; capture the trailing id as
        # the resume cursor. The *order* of these frames is covered as
        # primary behaviour by the sister test above, so a wrong order
        # here is a precondition failure, not the assertion under test.
        first_two = await _consume_sse_n(
            url_stream, 2, bearer=_bearer(tokens["beta"])
        )
        first_contents = [evt.get("content") for _id, evt in first_two]
        if first_contents != ["e1", "e2"]:
            raise RuntimeError(
                f"precondition: replay order wrong: {first_contents!r}"
            )
        # Third publish post-disconnect.
        async with httpx.AsyncClient(timeout=5.0) as ac:
            r3 = await ac.post(
                url_send,
                json=_send_payload("e3", from_agent="alpha"),
                headers=_bearer(tokens["alpha"]),
            )
        if r3.status_code != 200:
            raise RuntimeError(
                f"precondition publish e3 failed {r3.status_code}: {r3.text!r}"
            )
        cursor = first_two[-1][0]
        if cursor is None:
            raise RuntimeError(
                f"precondition: SSE id missing on replay frame: {first_two!r}"
            )
        # Reconnect with Last-Event-ID → expect ONLY e3.
        post_cursor = await _consume_sse_n(
            url_stream,
            1,
            bearer=_bearer(tokens["beta"]),
            last_event_id=cursor,
        )
        return post_cursor[0][1]

    # Act
    with _run_loopback(app, port):
        third = asyncio.run(driver())
    # Assert
    assert third.get("content") == "e3", (
        f"expected only e3 after Last-Event-ID; got {third!r}"
    )


# ---------------------------------------------------------------------------
# Case (i) — WI-1 finish-work: a DENIED publish must NOT persist to
# ``channel_events``. Denial is the policy working (handoff §0): it
# returns 403 to the sender and leaves zero side-effects on the bus or
# the durable store — otherwise a malicious / mis-permissioned sender
# could pollute the recipient's inbox by triggering the persist path.
# ---------------------------------------------------------------------------


def test_listen_denied_send_does_not_persist_to_channel_events(comms_env):
    """403'd cross-group send leaves ``channel_events`` empty for the target."""
    # Arrange
    import sqlite3

    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    # Act — alpha (group A) attempts gamma (group B); no grant exists.
    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            resp = c.post(
                f"http://127.0.0.1:{port}/agents/gamma/message:send",
                json=_send_payload("forbidden", from_agent="alpha"),
                headers=_bearer(tokens["alpha"]),
            )
    if resp.status_code != 403:
        raise RuntimeError(
            f"precondition: expected 403, got {resp.status_code}: {resp.text!r}"
        )

    # Assert — no channel_events row materialised for the denied target.
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM channel_events WHERE target = ?",
            ("gamma",),
        )
        n = int(cur.fetchone()[0])
    assert n == 0, (
        f"denied send must not persist; channel_events has {n} row(s) "
        "for target=gamma"
    )
