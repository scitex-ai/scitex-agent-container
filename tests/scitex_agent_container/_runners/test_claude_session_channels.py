"""``claude_session.run`` forwards channels + a2a_port to the conversation.

Locks the daemon-runner half of the sac-node-comms fix: ``run`` must
hand ``channels`` and ``a2a_port`` to the inbox-driven conversation task
so the SDK session can register the ``sac mcp channel`` adapter.
Real injected conversation/serve seams — no mocks.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import scitex_agent_container._runners.claude_session as runner


def _run_capturing_conversation(name: str, tmp_path: Path) -> dict[str, Any]:
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
        async def _stop_soon():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            name,
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12_345,
            channels=["server:sac"],
            run_conversation_fn=_fake_conv,
            serve_inbound_fn=_fake_serve,
        )

    asyncio.run(_scenario())
    return captured


def test_run_forwards_channels_to_conversation(tmp_path: Path) -> None:
    # Arrange
    name = "ag-chan-c"
    # Act
    captured = _run_capturing_conversation(name, tmp_path)
    # Assert
    assert captured["channels"] == ["server:sac"]


def test_run_forwards_a2a_port_to_conversation(tmp_path: Path) -> None:
    # Arrange
    name = "ag-chan-p"
    # Act
    captured = _run_capturing_conversation(name, tmp_path)
    # Assert
    assert captured["a2a_port"] == 12_345
