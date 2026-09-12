"""Synchronous client for the JSON-RPC session owned by Hermes' TUI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._hermes_tui_owner import GATEWAY_FILE


class HermesTuiRpcError(RuntimeError):
    """The live Hermes session could not accept an inbound event."""


@dataclass(frozen=True)
class HermesTurnActivity:
    """Authoritative live-turn observation from Hermes' session registry."""

    state: str
    session_status: str
    session_id: str

    @property
    def idle(self) -> bool:
        return self.state == "idle"


def _gateway_connection(state_dir: Path) -> tuple[str, str]:
    """Resolve the private websocket endpoint without exposing its bearer."""
    try:
        descriptor = json.loads((state_dir / GATEWAY_FILE).read_text(encoding="utf-8"))
        port = int(descriptor["port"])
        token = (state_dir / "hermes-api.key").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway state is unavailable: {exc}"
        ) from exc
    if not 0 < port < 65536 or len(token) < 16:
        raise HermesTuiRpcError("Hermes TUI gateway descriptor is invalid")
    return f"ws://127.0.0.1:{port}/api/ws?token={token}", token


def _connect(url: str, timeout_s: float, connect_fn: Any | None) -> Any:
    if connect_fn is None:
        try:
            from websockets.sync.client import connect as connect_fn
        except ImportError as exc:
            raise HermesTuiRpcError(
                "websockets>=15 is required for Hermes TUI delivery"
            ) from exc
    return connect_fn(url, open_timeout=timeout_s, close_timeout=1)


def active_sessions(
    state_dir: Path,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> list[dict]:
    """Return Hermes' authoritative process-local live-session snapshot.

    This deliberately does not activate a session.  The short-lived observer
    therefore cannot become a viewer, cancel an orphan reap, or receive the
    agent's token stream.
    """
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            result = _rpc(socket, 1, "session.active_list", {})
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    rows = result.get("sessions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HermesTuiRpcError(
            f"Hermes session.active_list returned malformed result: {result!r}"
        )
    return rows


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


def _select_session_row(rows: object, expected_title: str) -> dict:
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
    row = candidates[0]
    session_id = str(row.get("id") or "").strip()
    if not session_id:
        raise HermesTuiRpcError("Hermes active session has no id")
    return row


def _select_session(rows: object, expected_title: str) -> str:
    return str(_select_session_row(rows, expected_title)["id"])


def observe_turn_activity(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> HermesTurnActivity:
    """Read live turn activity without activating or mutating the session.

    ``session.active_list`` is Hermes' own synchronized session registry. It
    covers model/tool execution, approval/input boundaries, and construction
    before the first turn can safely be torn down.
    """
    rows = active_sessions(state_dir, timeout_s=timeout_s, connect_fn=connect_fn)
    row = _select_session_row(rows, f"sac:{agent_name}")
    status = str(row.get("status") or "").strip().lower()
    session_id = str(row.get("id") or "").strip()
    if status == "idle":
        state = "idle"
    elif status in {"working", "waiting", "starting"}:
        state = "active"
    else:
        raise HermesTuiRpcError(
            f"Hermes session {session_id!r} returned unknown activity status {status!r}"
        )
    return HermesTurnActivity(state=state, session_status=status, session_id=session_id)


def submit_turn(
    state_dir: Path,
    agent_name: str,
    text: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> str:
    """Submit through Hermes' native busy-input policy and return its status."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
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


__all__ = [
    "HermesTuiRpcError",
    "HermesTurnActivity",
    "active_sessions",
    "observe_turn_activity",
    "submit_turn",
]
