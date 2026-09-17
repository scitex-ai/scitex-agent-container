"""Native app-server admission for a visible Codex TUI.

The container shim owns one loopback WebSocket app-server and attaches the
visible TUI with ``codex --remote``. This module connects to that same server,
using ``turn/steer`` for an active turn and ``turn/start`` only while idle.
There is deliberately no terminal-key or FIFO-queue fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AgentConfig

ENDPOINT_FILENAME = "codex-app-server.endpoint"
_LOOPBACK_ENDPOINT = re.compile(r"^ws://127\.0\.0\.1:[1-9][0-9]{0,4}$")


@dataclass(frozen=True)
class NativeCodexAdmission:
    thread_id: str
    turn_id: str
    mode: str


def codex_app_server_endpoint_file(config: AgentConfig) -> Path:
    from .tui_session import state_dir_for_config

    return state_dir_for_config(config) / ENDPOINT_FILENAME


def codex_app_server_container_endpoint_file(config: AgentConfig) -> Path:
    """The same bound file as seen inside the agent container."""
    return Path("/state") / config.name / ENDPOINT_FILENAME


def _loaded_thread(threads: list[dict[str, Any]], *, agent: str) -> dict[str, Any]:
    loaded = [
        thread
        for thread in threads
        if (thread.get("status") or {}).get("type") in {"idle", "active", "systemError"}
    ]
    if len(loaded) != 1:
        ids = [str(thread.get("id", "?")) for thread in loaded]
        raise RuntimeError(
            f"Codex native delivery for {agent!r} requires exactly one loaded "
            f"thread on its private app-server; found {len(loaded)} ({ids})."
        )
    if (loaded[0].get("status") or {}).get("type") == "systemError":
        raise RuntimeError(
            f"Codex native delivery for {agent!r} refused a systemError thread."
        )
    return loaded[0]


class _Rpc:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        await self.websocket.send(
            json.dumps({"id": request_id, "method": method, "params": params})
        )
        while True:
            message = json.loads(await self.websocket.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex {method} returned no object result")
            return result


async def _admit_with_rpc(
    rpc: Any, *, agent: str, text: str
) -> NativeCodexAdmission:
    listed = await rpc.request("thread/list", {})
    thread = _loaded_thread(listed.get("data") or [], agent=agent)
    read = await rpc.request(
        "thread/read", {"threadId": thread["id"], "includeTurns": True}
    )
    current = read["thread"]
    active = [
        turn for turn in current.get("turns", []) if turn.get("status") == "inProgress"
    ]
    status = (current.get("status") or {}).get("type")
    input_items = [{"type": "text", "text": text}]
    if status == "active":
        if len(active) != 1:
            raise RuntimeError(
                f"Codex thread {current['id']} is active but exposes "
                f"{len(active)} in-progress turns; refusing ambiguous steer."
            )
        result = await rpc.request(
            "turn/steer",
            {
                "threadId": current["id"],
                "expectedTurnId": active[0]["id"],
                "input": input_items,
            },
        )
        if result.get("turnId") != active[0]["id"]:
            raise RuntimeError("Codex turn/steer acknowledged an unexpected turn")
        return NativeCodexAdmission(current["id"], result["turnId"], "steer")
    if status != "idle":
        raise RuntimeError(
            f"Codex thread {current['id']} is neither idle nor active ({status!r})."
        )
    result = await rpc.request(
        "turn/start", {"threadId": current["id"], "input": input_items}
    )
    return NativeCodexAdmission(current["id"], result["turn"]["id"], "start")


async def _deliver(endpoint: str, config: AgentConfig, text: str) -> NativeCodexAdmission:
    import websockets

    token_path = codex_app_server_endpoint_file(config).with_suffix(".token")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Codex app-server token is unavailable: {token_path}") from exc
    async with websockets.connect(
        endpoint,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=3,
        close_timeout=1,
    ) as websocket:
        rpc = _Rpc(websocket)
        await rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "scitex_agent_container",
                    "title": "SciTeX Agent Container turn bridge",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized"}))
        return await _admit_with_rpc(rpc, agent=config.name, text=text)


def deliver_native_codex_turn(config: AgentConfig, text: str) -> NativeCodexAdmission:
    """Admit ``text`` through the app-server shared with the visible TUI."""
    path = codex_app_server_endpoint_file(config)
    try:
        endpoint = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"Codex app-server endpoint is unavailable for {config.name!r}: {path}. "
            "Restart the managed TUI; terminal injection is not a fallback."
        ) from exc
    if not _LOOPBACK_ENDPOINT.fullmatch(endpoint):
        raise RuntimeError(f"Refusing invalid/non-loopback Codex endpoint: {endpoint!r}")
    port = int(endpoint.rsplit(":", 1)[1])
    if port > 65_535:
        raise RuntimeError(f"Refusing invalid Codex endpoint port: {port}")
    return asyncio.run(_deliver(endpoint, config, text))
