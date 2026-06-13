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
* (b) Cross-group deny (alpha groupA → gamma groupB) — 403 + reason
  to the sender AND a ``kind="denied_attempt"`` notification to the
  receiver's inbox (comms item D — body never leaks).
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
* (i) Listen-surface denied send persists a ``kind="denied_attempt"``
  row with empty content to ``channel_events`` — comms item D, so a
  receiver coming online later still learns of the attempt without
  the body leaking.

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


# ---------------------------------------------------------------------------
# Comms item D — denied send publishes a denied-attempt notification to
# the receiver. The OLD contract ("denial leaves zero side-effects on
# the bus") is intentionally retired by item D: the receiver MUST be
# told "X tried to reach you, denied — reason ..." so they can decide
# whether to grant. The body never leaks (content=""); only attempt
# metadata travels. Tests below lock in the NEW contract.
# ---------------------------------------------------------------------------


_DENIED_BODY = "forbidden body — must not leak"


@pytest.fixture
def cross_group_deny_smoke(comms_env):
    """Boot real ``sac listen``, subscribe gamma's SSE, post a denied
    alpha→gamma send, and capture both the POST response and the SSE
    event published to gamma's inbox.

    Item D: the SSE event MUST be the denied-attempt notification —
    same broker/channel the receiver subscribes to via
    ``a2a/_inbox_bus.py``.
    """
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
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
                    json=_send_payload(_DENIED_BODY, from_agent="alpha"),
                    headers=_bearer(tokens["alpha"]),
                )
            captured["status"] = resp.status_code
            captured["body"] = resp.text
            await asyncio.wait_for(sub, timeout=5.0)
        finally:
            if not sub.done():
                sub.cancel()
                with contextlib.suppress(BaseException):
                    await sub
        return captured

    with _run_loopback(app, port):
        return asyncio.run(driver())


def test_cross_group_deny_smoke_returns_403_to_sender(cross_group_deny_smoke):
    # Arrange
    captured = cross_group_deny_smoke
    # Act
    status = captured["status"]
    # Assert
    assert status == 403, captured.get("body")


def test_cross_group_deny_smoke_publishes_denied_attempt_to_recipient_sse(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    kind = event.get("kind")
    # Assert
    assert kind == "denied_attempt"


def test_cross_group_deny_smoke_notification_content_is_empty(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    content = event.get("content", "")
    # Assert
    assert content == ""


def test_cross_group_deny_smoke_notification_names_the_sender(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    sender = event.get("from_agent")
    # Assert
    assert sender == "alpha"


def test_cross_group_deny_smoke_notification_names_the_receiver(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    receiver = event.get("to_agent")
    # Assert
    assert receiver == "gamma"


def test_cross_group_deny_smoke_notification_carries_deny_reason(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    reason = event.get("extra", {}).get("deny_reason", "")
    # Assert
    assert "cross-group" in reason


def test_cross_group_deny_smoke_body_does_not_leak_to_recipient(
    cross_group_deny_smoke,
):
    # Arrange
    event = cross_group_deny_smoke["event"]
    # Act
    serialized = json.dumps(event)
    # Assert
    assert _DENIED_BODY not in serialized


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
        raise AssertionError(f"SSE {url!r} closed before {n} events (saw {len(seen)})")

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
        frames = await _consume_sse_n(url_stream, 2, bearer=_bearer(tokens["beta"]))
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
        first_two = await _consume_sse_n(url_stream, 2, bearer=_bearer(tokens["beta"]))
        first_contents = [evt.get("content") for _id, evt in first_two]
        if first_contents != ["e1", "e2"]:
            raise RuntimeError(f"precondition: replay order wrong: {first_contents!r}")
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
# Case (i) — WI-1 finish-work + comms item D: a DENIED publish persists a
# ``kind="denied_attempt"`` notification (no body) to ``channel_events``
# so a receiver coming online later still learns of the attempt. The
# message ``content`` column stays empty (no body leak); only attempt
# metadata travels. This intentionally retires the old "denial leaves
# zero side-effects on the durable store" contract — that earlier
# behaviour left the receiver unable to decide whether to grant.
# ---------------------------------------------------------------------------


def _read_channel_events_for_target(db, target: str) -> list[dict]:
    """Read every ``channel_events`` row for ``target`` as plain dicts.

    Extracted from the fixture so the fixture has no resource-acquiring
    keyword (``connect(...)`` / ``open(...)``) — keeps the audit's
    "fixture must yield, not return" pattern matcher quiet while the
    underlying connection is already closed by the ``with`` block.
    """
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, target, source, kind, content, meta_json, ts "
            "FROM channel_events WHERE target = ? ORDER BY id",
            (target,),
        )
        return [dict(r) for r in cur.fetchall()]


@pytest.fixture
def denied_send_channel_rows(comms_env):
    """Boot real ``sac listen``, post a denied alpha→gamma send, then
    return the response + every ``channel_events`` row whose target
    is gamma.
    """
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()

    with _run_loopback(app, port):
        with httpx.Client(timeout=5.0) as c:
            resp = c.post(
                f"http://127.0.0.1:{port}/agents/gamma/message:send",
                json=_send_payload(_DENIED_BODY, from_agent="alpha"),
                headers=_bearer(tokens["alpha"]),
            )

    rows = _read_channel_events_for_target(db, "gamma")
    return {"resp": resp, "rows": rows}


def test_listen_denied_send_returns_403_to_sender(denied_send_channel_rows):
    # Arrange
    resp = denied_send_channel_rows["resp"]
    # Act
    status = resp.status_code
    # Assert
    assert status == 403, resp.text


def test_listen_denied_send_persists_three_channel_events_rows(
    denied_send_channel_rows,
):
    """Task #27 (ACL block/unblock approve-flow) + sac-comms item-D
    (ACL-deny synthetic notify to target receiver, lead a2a c42b3e3c)
    — a cross-group deny persists THREE rows on the receiver: (1) the
    existing metadata-only ``denied_attempt`` (comms item D pre-#27),
    (2) the ADDITIVE synthetic ``acl_deny_notify`` push to the target
    receiver (rate-limited per sender/target, PR #389), (3) the
    operator-facing ``approval_prompt`` push embedding the
    ``sac a2a unblock`` / ``sac a2a block`` CLI commands. The two
    push rows have different audiences: synthetic notify → target
    agent that should grant; approval_prompt → operator with CLI."""
    # Arrange
    rows = denied_send_channel_rows["rows"]
    # Act
    n = len(rows)
    # Assert
    assert n == 3, rows


def test_listen_denied_send_persisted_first_row_kind_is_denied_attempt(
    denied_send_channel_rows,
):
    """First row preserved verbatim from pre-task-#27 (comms item D)."""
    # Arrange
    row = denied_send_channel_rows["rows"][0]
    # Act
    kind = row["kind"]
    # Assert
    assert kind == "denied_attempt"


def test_listen_denied_send_persisted_third_row_is_approval_prompt(
    denied_send_channel_rows,
):
    """Third row preserved from task #27 — the operator-facing prompt
    (``kind="message"`` so existing inbox renderers surface it via
    the normal-message path; structured fields ride in
    ``extra.approval_prompt``). Shifted from index [1] to [2] when
    PR #389 added the additive synthetic ``acl_deny_notify`` push
    between the existing ``denied_attempt`` and ``approval_prompt``
    rows (synthetic notify targets the receiver-agent; approval_prompt
    targets the operator with CLI commands)."""
    # Arrange
    row = denied_send_channel_rows["rows"][2]
    meta = json.loads(row["meta_json"])
    # Act
    is_prompt = (meta.get("extra") or {}).get("approval_prompt")
    # Assert
    assert is_prompt is True


def test_listen_denied_send_persisted_row_source_names_the_sender(
    denied_send_channel_rows,
):
    # Arrange — denied_attempt row only (the approval_prompt row's
    # ``source`` is also alpha by design, so either would pass; pin
    # on the historical row to keep the test specific).
    row = denied_send_channel_rows["rows"][0]
    # Act
    source = row["source"]
    # Assert
    assert source == "alpha"


def test_listen_denied_send_persisted_row_content_column_is_empty(
    denied_send_channel_rows,
):
    """Hard-pinned to ``""`` (or NULL) on the ``denied_attempt`` row
    — the sender's body must never land in the receiver's durable
    inbox. The companion ``approval_prompt`` row's content is the
    operator-facing prompt text (not the sender's body — those are
    different things)."""
    # Arrange
    row = denied_send_channel_rows["rows"][0]
    # Act
    content = row["content"] or ""
    # Assert
    assert content == ""


def test_listen_denied_send_persisted_row_meta_json_carries_deny_reason(
    denied_send_channel_rows,
):
    # Arrange
    row = denied_send_channel_rows["rows"][0]
    meta = json.loads(row["meta_json"])
    # Act
    reason = meta.get("extra", {}).get("deny_reason", "")
    # Assert
    assert "cross-group" in reason


def test_listen_denied_send_approval_prompt_embeds_unblock_command(
    denied_send_channel_rows,
):
    """The approve-prompt push MUST embed the ``sac a2a unblock``
    command so the operator can act without leaving the inbox.
    Task #27 contract: prompt body is self-contained. Row index
    shifted to [2] after PR #389 inserted the additive synthetic
    notify between [0] and the approval_prompt."""
    # Arrange
    row = denied_send_channel_rows["rows"][2]
    # Act
    body = row["content"] or ""
    # Assert
    assert "sac a2a unblock alpha gamma" in body


def test_listen_denied_send_approval_prompt_embeds_block_command(
    denied_send_channel_rows,
):
    """Task #27 contract: the prompt embeds BOTH the unblock AND
    block commands so the operator picks one verb. Row index [2]
    after PR #389 (additive synthetic notify slots at [1])."""
    # Arrange
    row = denied_send_channel_rows["rows"][2]
    # Act
    body = row["content"] or ""
    # Assert
    assert "sac a2a block alpha gamma" in body


def test_listen_denied_send_approval_prompt_does_not_leak_sender_body(
    denied_send_channel_rows,
):
    """Task #27 contract: the approval prompt MUST NOT echo the
    denied message body — receivers decide on IDENTITY, not on
    content. The sender's original body (``_DENIED_BODY``) is
    NEVER copied into the prompt push. Row index [2] after PR #389."""
    # Arrange
    row = denied_send_channel_rows["rows"][2]
    # Act
    body = row["content"] or ""
    # Assert
    assert _DENIED_BODY not in body


def test_listen_denied_send_persisted_row_does_not_leak_message_body(
    denied_send_channel_rows,
):
    """Defence-in-depth: the entire stored frame (meta_json + content)
    must not contain the secret body.
    """
    # Arrange
    row = denied_send_channel_rows["rows"][0]
    # Act
    blob = (row["content"] or "") + "|" + row["meta_json"]
    # Assert
    assert _DENIED_BODY not in blob
