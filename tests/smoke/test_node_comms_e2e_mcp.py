"""Smoke layer: end-to-end coverage of the node-comms substrate —
**MCP tool** pattern.

The sibling ``test_node_comms_e2e_http.py`` drives the substrate at
the wire level (raw ``httpx`` POST + SSE). This file drives the same
real ``sac listen`` (real ``state.db``, real per-node bearer tokens,
real WI-2 ACL) through the layer an agent actually uses: the
``a2a_*`` MCP tools registered by ``_mcp/channel.py::_register_tools``
(``sac mcp channel --name <node>``). No mocks — the only test double
is a structural ``_ToolRecorder`` that captures the
``@server.list_tools()`` / ``@server.call_tool()`` registration
contract so the registered callables can be invoked directly (the
same collaborator ``tests/.../_mcp/test_channel.py`` uses).

What the MCP layer adds over the HTTP layer (why both exist):

* the tool sets ``from_agent`` itself (tools-as-contract) — case (d)
  shows a *misconfigured* node (claims X, authenticates as Y) is
  still rejected by the server's spoof gate;
* the receive side projects a bus event onto the Claude-channel
  notification shape via ``_build_notification`` — case (a) asserts
  that projection end-to-end;
* ``a2a_reply`` looks the original sender up by ``msg_id`` from the
  receive-side ring buffer — the reply round-trip case exercises it.

Cases (mirroring the HTTP file's ACL matrix, through the tools):

* (a) same-group ``a2a_send`` delivers + projects a notification.
* (b) cross-group ``a2a_send`` denied — tool surfaces 403 + reason.
* (c) ``grant_send`` unblocks the cross-group ``a2a_send``.
* (d) misconfigured node (claims ``beta``, bearer is ``alpha``) —
  server returns 403 "identity spoof".
* (e) 4-sibling fan-out — every pair delivers via the tool.
* (reply) receive → ``a2a_reply`` routes back to the original sender.

Replay-on-reconnect lives only on the ``a2a/_server.py`` surface,
which the MCP tools do not target, so it is covered once, in the
HTTP file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator

import pytest

mcp_types = pytest.importorskip("mcp.types")  # gates module on `mcp` install

from scitex_agent_container._listen.server import create_app  # noqa: E402
from scitex_agent_container._mcp.channel import (  # noqa: E402
    _build_notification,
    _recent,
    _register_tools,
)
from scitex_agent_container._state.state_db_nodes import grant_send  # noqa: E402
from tests.smoke._node_comms import (  # noqa: E402
    _await_subscribed_and_read_one,
    _bearer,
    _free_port,
    _run_loopback,
    _set_up_four_siblings,
    _set_up_two_groups,
)

pytestmark = pytest.mark.smoke


class _ToolRecorder:
    """Captures ``list_tools`` / ``call_tool`` registrations so the
    real ``_register_tools`` runs unchanged and the captured callables
    can be invoked directly. Not a mock — plain attribute storage.
    """

    def __init__(self) -> None:
        self.list_tools_fn = None
        self.call_tool_fn = None

    def list_tools(self):
        def _decorate(fn):
            self.list_tools_fn = fn
            return fn

        return _decorate

    def call_tool(self):
        def _decorate(fn):
            self.call_tool_fn = fn
            return fn

        return _decorate


def _tools_for(name: str, listen_url: str, bearer: str) -> _ToolRecorder:
    """Build the ``a2a_*`` tool surface a node would expose via
    ``sac mcp channel --name <name>``, pointed at the live listen."""
    rec = _ToolRecorder()
    _register_tools(rec, agent_name=name, listen_url=listen_url, bearer=bearer)
    return rec


async def _call(rec: _ToolRecorder, tool: str, args: dict) -> dict:
    """Invoke a registered tool and decode its single JSON TextContent."""
    out = await rec.call_tool_fn(tool, args)
    return json.loads(out[0].text)


@pytest.fixture(autouse=True)
def _clear_recent_ring() -> Iterator[None]:
    """The receive-side inbox ring is module-global; isolate each test."""
    _recent.clear()
    yield
    _recent.clear()


# ---------------------------------------------------------------------------
# Case (a) — same-group a2a_send delivers + projects a channel notification
# ---------------------------------------------------------------------------


def test_a2a_send_tool_same_group_delivers_and_projects_notification(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def driver() -> dict:
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"{base}/agents/beta/inbox/stream",
                headers=_bearer(tokens["beta"]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            rec = _tools_for("alpha", base, tokens["alpha"])
            result = await _call(
                rec, "a2a_send", {"target": "beta", "content": "hello via mcp"}
            )
            if result.get("status") != 200:
                raise RuntimeError(f"a2a_send tool returned {result!r}")
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
    notif = _build_notification(event)
    # Assert — delivered content survives, and the projection names the sender.
    assert notif["content"] == "hello via mcp" and notif["meta"]["source"] == "alpha"


# ---------------------------------------------------------------------------
# Case (b) — cross-group a2a_send denied: tool surfaces 403 + reason
# ---------------------------------------------------------------------------


def test_a2a_send_tool_cross_group_denied_with_reason(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def driver() -> dict:
        rec = _tools_for("alpha", base, tokens["alpha"])
        return await _call(rec, "a2a_send", {"target": "gamma", "content": "forbidden"})

    # Act
    with _run_loopback(app, port):
        result = asyncio.run(driver())
    # Assert
    body = result.get("body") or {}
    assert result.get("status") == 403 and "cross-group" in (
        body.get("reason") or ""
    ), f"unexpected tool result: {result!r}"


# ---------------------------------------------------------------------------
# Case (c) — grant unblocks the cross-group a2a_send
# ---------------------------------------------------------------------------


def test_a2a_send_tool_cross_group_after_grant_delivers(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    grant_send(sender="alpha", target="gamma", db_path=db, note="mcp-smoke grant")
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def driver() -> dict:
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"{base}/agents/gamma/inbox/stream",
                headers=_bearer(tokens["gamma"]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            rec = _tools_for("alpha", base, tokens["alpha"])
            result = await _call(
                rec, "a2a_send", {"target": "gamma", "content": "granted via mcp"}
            )
            if result.get("status") != 200:
                raise RuntimeError(f"granted a2a_send returned {result!r}")
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
    assert event.get("content") == "granted via mcp"


# ---------------------------------------------------------------------------
# Case (d) — misconfigured node: claims "beta" but its bearer is alpha's.
# The tool sets from_agent="beta"; the server's spoof gate rejects it.
# ---------------------------------------------------------------------------


def test_a2a_send_tool_identity_spoof_returns_403(comms_env):
    # Arrange — register beta's tool surface but hand it alpha's bearer.
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def driver() -> dict:
        rec = _tools_for("beta", base, tokens["alpha"])
        return await _call(rec, "a2a_send", {"target": "beta", "content": "spoofed"})

    # Act
    with _run_loopback(app, port):
        result = asyncio.run(driver())
    # Assert
    body = result.get("body") or {}
    assert result.get("status") == 403 and "identity spoof" in (
        body.get("reason") or ""
    ), f"unexpected tool result: {result!r}"


# ---------------------------------------------------------------------------
# Case (e) — 4-sibling fan-out: every ordered pair delivers via the tool
# ---------------------------------------------------------------------------


def test_a2a_send_tool_sibling_fan_out_every_pair_delivers(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_four_siblings(db)
    children = ("alpha", "beta", "gamma", "zeta")
    pairs = [(s, t) for s in children for t in children if s != t]
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def drive_one(sender: str, target: str) -> str | None:
        ready = asyncio.Event()
        captured: dict = {}

        async def consume():
            captured["event"] = await _await_subscribed_and_read_one(
                f"{base}/agents/{target}/inbox/stream",
                headers=_bearer(tokens[target]),
                ready=ready,
            )

        sub = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5.0)
            rec = _tools_for(sender, base, tokens[sender])
            result = await _call(
                rec,
                "a2a_send",
                {"target": target, "content": f"mcp-{sender}-{target}"},
            )
            if result.get("status") != 200:
                return f"{sender}->{target} tool result {result!r}"
            await asyncio.wait_for(sub, timeout=5.0)
            event = captured.get("event") or {}
            if event.get("content") != f"mcp-{sender}-{target}":
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
# (reply) — receive a message, then a2a_reply routes back to its sender
# ---------------------------------------------------------------------------


def test_a2a_reply_tool_routes_back_to_original_sender(comms_env):
    # Arrange
    db = comms_env["db"]
    tokens = _set_up_two_groups(db)
    app = create_app(token=tokens["host"], local_host="smoke-local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    async def driver() -> dict:
        # 1. beta subscribes; alpha sends it a message via the tool.
        beta_ready = asyncio.Event()
        beta_box: dict = {}

        async def beta_consume():
            beta_box["event"] = await _await_subscribed_and_read_one(
                f"{base}/agents/beta/inbox/stream",
                headers=_bearer(tokens["beta"]),
                ready=beta_ready,
            )

        beta_sub = asyncio.create_task(beta_consume())
        try:
            await asyncio.wait_for(beta_ready.wait(), timeout=5.0)
            alpha = _tools_for("alpha", base, tokens["alpha"])
            sent = await _call(
                alpha,
                "a2a_send",
                {"target": "beta", "content": "ping", "requires_reply": True},
            )
            if sent.get("status") != 200:
                raise RuntimeError(f"initial a2a_send returned {sent!r}")
            await asyncio.wait_for(beta_sub, timeout=5.0)
        finally:
            if not beta_sub.done():
                beta_sub.cancel()
                with contextlib.suppress(BaseException):
                    await beta_sub

        received = beta_box.get("event") or {}
        # The receive-side adapter buffers each event into ``_recent`` so
        # a2a_reply can resolve the sender by msg_id; replicate that step.
        _recent.append(received)

        # 2. alpha subscribes; beta replies via a2a_reply.
        alpha_ready = asyncio.Event()
        alpha_box: dict = {}

        async def alpha_consume():
            alpha_box["event"] = await _await_subscribed_and_read_one(
                f"{base}/agents/alpha/inbox/stream",
                headers=_bearer(tokens["alpha"]),
                ready=alpha_ready,
            )

        alpha_sub = asyncio.create_task(alpha_consume())
        try:
            await asyncio.wait_for(alpha_ready.wait(), timeout=5.0)
            beta = _tools_for("beta", base, tokens["beta"])
            replied = await _call(
                beta,
                "a2a_reply",
                {"in_reply_to": received.get("msg_id"), "content": "pong"},
            )
            if replied.get("status") != 200:
                raise RuntimeError(f"a2a_reply returned {replied!r}")
            await asyncio.wait_for(alpha_sub, timeout=5.0)
        finally:
            if not alpha_sub.done():
                alpha_sub.cancel()
                with contextlib.suppress(BaseException):
                    await alpha_sub
        return alpha_box.get("event", {})

    # Act
    with _run_loopback(app, port):
        reply_event = asyncio.run(driver())
    # Assert — the reply reached alpha (the original sender).
    assert reply_event.get("content") == "pong"
