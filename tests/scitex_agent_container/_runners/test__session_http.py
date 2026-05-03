"""Smoke tests for the inbound-turn HTTP endpoint.

Drives ``serve_inbound`` with a synthetic inbox + a tiny consumer task
that mimics the SDK conversation: dequeue envelope, set its future
with a canned reply. Asserts POST /v1/turn round-trips through the
queue and returns the reply as JSON.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from scitex_agent_container._runners._session_http import serve_inbound
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


def _free_port() -> int:
    """Ask the kernel for an unused TCP port (race-y but adequate for a test)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fake_consumer(inbox: "asyncio.Queue", *, reply_map: dict) -> None:
    """Stand in for the real conversation task: pop turn envelopes and
    resolve the future from a canned reply map."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_result(reply_map.get(env.text, f"echo:{env.text}"))


class TestServeInbound:
    def test_post_v1_turn_roundtrip(self) -> None:
        """POST /v1/turn → consumer drains envelope → 200 with reply."""
        port = _free_port()
        replies = {"hello": "world"}

        async def _scenario() -> dict:
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map=replies))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            # Wait for the server to bind.
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("server never bound")

            def _do_post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/turn",
                    data=b'{"text": "hello"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    import json as _json

                    return resp.status, _json.loads(resp.read().decode())

            status, body = await asyncio.to_thread(_do_post)

            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return {"status": status, "body": body}

        result = asyncio.run(_scenario())
        assert result["status"] == 200
        assert result["body"]["reply"] == "world"
        assert result["body"]["exit_after"] is False

    def test_post_v1_turn_rejects_missing_text(self) -> None:
        """Empty body → 400."""
        port = _free_port()

        async def _scenario() -> int:
            import urllib.error
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map={}))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _do_post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/turn",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            code = await asyncio.to_thread(_do_post)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return code

        assert asyncio.run(_scenario()) == 400

    def test_health_endpoint(self) -> None:
        """GET /health → {status: ok}."""
        port = _free_port()

        async def _scenario() -> dict:
            import json as _json
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map={}))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _get():
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=5.0
                ) as resp:
                    return _json.loads(resp.read().decode())

            body = await asyncio.to_thread(_get)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return body

        assert asyncio.run(_scenario()) == {"status": "ok"}
