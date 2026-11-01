"""``claude_session.run`` forwards channels + a2a_port to the conversation.

Locks the daemon-runner half of the sac-node-comms fix: ``run`` must
hand ``channels`` and ``a2a_port`` to the inbox-driven conversation task
so the SDK session can register the ``sac mcp channel`` adapter.
Real injected conversation/serve seams and a local SSE listener — no mocks.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner


def _run_capturing_conversation(
    name: str, tmp_path: Path, env_save_restore
) -> dict[str, Any]:
    """Run ``run`` once with capturing conversation/serve seams.

    Returns the kwargs the conversation fn observed so each assertion
    test can check a single field.
    """
    captured: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop, **kw):
        await stop.wait()

    async def _fake_conv(_name, _state_dir, **kw):
        captured["channels"] = kw.get("channels")
        captured["a2a_port"] = kw.get("a2a_port")
        await kw["stop"].wait()

    async def _scenario() -> int:
        connected = asyncio.Event()

        async def _local_sse(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b": contract-ready\n\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            connected.set()

        server = await asyncio.start_server(_local_sse, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        config = tmp_path / "listen.yaml"
        config.write_text(f"listen:\n  host: 127.0.0.1\n  port: {port}\n")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(config))
        env_save_restore.set("SAC_LISTEN_BEARER", "channel-contract-fixture")

        async def _stop_when_connected():
            # Shutdown starts after the real consumer reaches our listener,
            # rather than racing a wall-clock delay against startup.
            await asyncio.wait_for(connected.wait(), timeout=5)
            os.kill(os.getpid(), signal.SIGTERM)

        async with server, asyncio.TaskGroup() as tasks:
            tasks.create_task(_stop_when_connected())
            daemon = tasks.create_task(
                runner.run(
                    name,
                    state_root=tmp_path,
                    tick_seconds=0.01,
                    a2a_port=12_345,
                    channels=["server:sac"],
                    run_conversation_fn=_fake_conv,
                    serve_inbound_fn=_fake_serve,
                )
            )
        return daemon.result()

    asyncio.run(_scenario())
    return captured


def test_run_forwards_channels_to_conversation(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    name = "ag-chan-c"
    # Act
    captured = _run_capturing_conversation(name, tmp_path, env_save_restore)
    # Assert
    assert captured["channels"] == ["server:sac"]


def test_run_forwards_a2a_port_to_conversation(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    name = "ag-chan-p"
    # Act
    captured = _run_capturing_conversation(name, tmp_path, env_save_restore)
    # Assert
    assert captured["a2a_port"] == 12_345
