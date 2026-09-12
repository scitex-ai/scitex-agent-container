"""Synchronous client for the JSON-RPC session owned by Hermes' TUI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._hermes_tui_owner import GATEWAY_FILE


class HermesTuiRpcError(RuntimeError):
    """The live Hermes session could not accept an inbound event."""


def _rpc(socket: Any, request_id: int, method: str, params: dict) -> dict:
    socket.send(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
    )
    while True:
        raw = json.loads(socket.recv())
        if raw.get("id") != request_id:
            continue
        if isinstance(raw.get("error"), dict):
            error = raw["error"]
            raise HermesTuiRpcError(
                f"Hermes {method} refused: {error.get('message', error)!s}"
            )
        result = raw.get("result")
        if not isinstance(result, dict):
            raise HermesTuiRpcError(
                f"Hermes {method} returned malformed result: {raw!r}"
            )
        return result


def _select_session(rows: object, expected_title: str) -> str:
    sessions = (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )
    exact = [
        row
        for row in sessions
        if row.get("title") == expected_title
        or row.get("session_key") == expected_title
    ]
    candidates = exact or sessions
    if len(candidates) != 1:
        raise HermesTuiRpcError(
            f"cannot identify one live Hermes session for {expected_title!r}: "
            f"{len(exact)} exact matches among {len(sessions)} live sessions"
        )
    session_id = str(candidates[0].get("id") or "").strip()
    if not session_id:
        raise HermesTuiRpcError("Hermes active session has no id")
    return session_id


def submit_turn(
    state_dir: Path,
    agent_name: str,
    text: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> str:
    """Submit through Hermes' native busy-input policy and return its status."""
    try:
        descriptor = json.loads((state_dir / GATEWAY_FILE).read_text(encoding="utf-8"))
        port = int(descriptor["port"])
        token = (state_dir / "hermes-api.key").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway state is unavailable: {exc}"
        ) from exc
    if connect_fn is None:
        try:
            from websockets.sync.client import connect as connect_fn
        except ImportError as exc:
            raise HermesTuiRpcError(
                "websockets>=15 is required for Hermes TUI delivery"
            ) from exc
    url = f"ws://127.0.0.1:{port}/api/ws?token={token}"
    try:
        with connect_fn(url, open_timeout=timeout_s, close_timeout=1) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session_id = _select_session(listing.get("sessions"), f"sac:{agent_name}")
            _rpc(
                socket,
                2,
                "session.activate",
                {"session_id": session_id, "omit_messages": True},
            )
            result = _rpc(
                socket, 3, "prompt.submit", {"session_id": session_id, "text": text}
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url} is unreachable: {exc}"
        ) from exc
    status = str(result.get("status") or "").strip()
    if status not in {"streaming", "steered", "queued", "redirected"}:
        raise HermesTuiRpcError(f"Hermes prompt.submit was not accepted: {result!r}")
    return status


__all__ = ["HermesTuiRpcError", "submit_turn"]
