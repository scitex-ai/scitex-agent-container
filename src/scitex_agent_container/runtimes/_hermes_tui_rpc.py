"""Synchronous client for the JSON-RPC session owned by Hermes' TUI."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

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


@dataclass(frozen=True)
class HermesVisibleTurnReceipt:
    """Native acceptance plus the Hermes-owned visibility proof."""

    status: str
    visibility: str
    session_id: str


_SEARCH_RESPONSE_MAX_BYTES = 256 * 1024


def _detailed_health(
    port: int,
    token: str,
    *,
    timeout_s: float = 2.0,
    urlopen_fn: Any = urlopen,
) -> dict[str, Any]:
    """Return Hermes' authenticated readiness document or fail closed."""
    request = Request(
        f"http://127.0.0.1:{port}/health/detailed",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen_fn(request, timeout=timeout_s) as response:
            payload = json.loads(response.read())
            status = int(response.status)
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes authenticated readiness is unavailable: {exc}"
        ) from exc
    if status != 200 or not isinstance(payload, dict):
        raise HermesTuiRpcError(
            "Hermes authenticated readiness returned a malformed response"
        )
    if payload.get("status") != "ok":
        raise HermesTuiRpcError(
            "Hermes authenticated readiness is degraded: "
            f"{payload.get('readiness', payload)!r}"
        )
    return payload


def gateway_detailed_health(
    state_dir: Path,
    *,
    timeout_s: float = 2.0,
    urlopen_fn: Any = urlopen,
) -> dict[str, Any]:
    """Read authenticated readiness for the exact published gateway owner."""
    _url, token = _gateway_connection(state_dir)
    try:
        descriptor = json.loads((state_dir / GATEWAY_FILE).read_text(encoding="utf-8"))
        port = int(descriptor["port"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway state is unavailable: {exc}"
        ) from exc
    return _detailed_health(
        port,
        token,
        timeout_s=timeout_s,
        urlopen_fn=urlopen_fn,
    )


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


def pause_heartbeat(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> str:
    """Pause Hermes' session heartbeat without creating a model turn.

    ``prompt.submit('/heartbeat pause')`` does *not* execute a slash command.
    It appends an ordinary user message and therefore wakes the model with the
    complete conversation.  Hermes exposes heartbeat state through its
    intent-level ``session.control`` RPC; use that control-plane operation so
    SAC's deterministic inbox sidecar can remain active while an idle session
    consumes no inference slot.
    """
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session_id = _select_session(listing.get("sessions"), f"sac:{agent_name}")
            result = _rpc(
                socket,
                2,
                "session.control",
                {"session_id": session_id, "action": "heartbeat.pause"},
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    control = result.get("control")
    if not isinstance(control, dict):
        raise HermesTuiRpcError(
            f"Hermes session.control returned malformed result: {result!r}"
        )
    heartbeat = control.get("heartbeat")
    if heartbeat is None:
        return "absent"
    if not isinstance(heartbeat, dict) or heartbeat.get("status") != "paused":
        raise HermesTuiRpcError(f"Hermes heartbeat did not pause: {heartbeat!r}")
    return "paused"


def _delivery_visibility(payload: object, delivery_id: str) -> str | None:
    """Name the Hermes projection containing one durable delivery marker.

    Only user-originated fields count.  An assistant quoting the marker is not
    proof that Hermes accepted that marker as input, while ``messages``, the
    live turn's ``user``/``corrections``, and the native queue are precisely
    the fields the official clients render as user input.
    """
    if not isinstance(payload, dict):
        return None
    marker = f"<!-- delivery:{delivery_id} -->"
    messages = payload.get("messages")
    if isinstance(messages, list) and any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and marker in str(message.get("text") or "")
        for message in messages
    ):
        return "session.messages"
    inflight = payload.get("inflight")
    if isinstance(inflight, dict):
        if marker in str(inflight.get("user") or ""):
            return "session.inflight.user"
        corrections = inflight.get("corrections")
        if isinstance(corrections, list) and any(
            marker in str(correction) for correction in corrections
        ):
            return "session.inflight.corrections"
    queued = payload.get("queued")
    if isinstance(queued, dict) and marker in str(queued.get("user") or ""):
        return "session.queued.user"
    return None


def _stored_delivery_visibility(
    gateway_url: str,
    token: str,
    *,
    session_key: str,
    delivery_id: str,
    timeout_s: float,
    urlopen_fn: Any = urlopen,
) -> str | None:
    """Use Hermes' bounded FTS projection to find an older accepted input.

    ``session.activate(omit_messages=False)`` reconstructs and serializes the
    complete display lineage.  The management search endpoint instead performs
    an indexed lookup and returns at most one small result for this delivery's
    conversation.  It is therefore safe for sessions whose transcript no
    longer fits the websocket implementation's frame limit.
    """
    endpoint = urlsplit(gateway_url)
    # FTS tokenizes the marker's colon as a separator.  Query the quoted
    # two-token phrase, then verify the literal marker in the returned snippet;
    # an unrelated occurrence of the opaque id cannot become delivery proof.
    exact_query = json.dumps(f"delivery {delivery_id}")
    search_url = (
        f"http://{endpoint.hostname}:{endpoint.port}/api/sessions/search"
        f"?q={quote(exact_query, safe='')}&limit=20"
    )
    request = Request(
        search_url,
        headers={"X-Hermes-Session-Token": token},
        method="GET",
    )
    try:
        with urlopen_fn(request, timeout=timeout_s) as response:
            encoded = response.read(_SEARCH_RESPONSE_MAX_BYTES + 1)
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes bounded delivery search is unavailable: {exc}"
        ) from exc
    if len(encoded) > _SEARCH_RESPONSE_MAX_BYTES:
        raise HermesTuiRpcError("Hermes bounded delivery search response is too large")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise HermesTuiRpcError(
            f"Hermes bounded delivery search returned malformed JSON: {exc}"
        ) from exc
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise HermesTuiRpcError(
            f"Hermes bounded delivery search returned malformed result: {payload!r}"
        )
    marker = f"delivery:{delivery_id}"
    for row in rows:
        if not isinstance(row, dict):
            continue
        snippet = str(row.get("snippet") or "").replace(">>>", "").replace("<<<", "")
        if (
            row.get("role") == "user"
            and marker in snippet
            and session_key
            in {str(row.get("session_id") or ""), str(row.get("lineage_root") or "")}
        ):
            return "session.search"
    return None


def submit_visible_turn(
    state_dir: Path,
    agent_name: str,
    text: str,
    *,
    delivery_id: str,
    timeout_s: float = 10.0,
    max_observations: int = 20,
    poll_s: float = 0.1,
    connect_fn: Any | None = None,
    sleep_fn: Any = time.sleep,
    urlopen_fn: Any = urlopen,
) -> HermesVisibleTurnReceipt:
    """Idempotently submit and prove a visible user input via Hermes JSON-RPC.

    ``prompt.submit`` is the sole mutation.  The surrounding lightweight
    ``session.activate(omit_messages=True)`` calls expose inflight/queue input;
    Hermes' indexed management search proves an older persisted input on a
    retry.  No call serializes the complete transcript.  If acceptance or
    visibility cannot be proved, the caller fails closed and its durable
    notification remains unconfirmed.
    """
    delivery_id = str(delivery_id or "").strip()
    if not delivery_id:
        raise HermesTuiRpcError("visible Hermes delivery requires a delivery id")
    if max_observations < 1:
        raise ValueError("max_observations must be positive")
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session_id = _select_session(listing.get("sessions"), f"sac:{agent_name}")
            before = _rpc(
                socket,
                2,
                "session.activate",
                {"session_id": session_id, "omit_messages": True},
            )
            if visibility := _delivery_visibility(before, delivery_id):
                return HermesVisibleTurnReceipt(
                    status="already_visible",
                    visibility=visibility,
                    session_id=session_id,
                )
            session_key = str(before.get("session_key") or "").strip()
            if not session_key:
                raise HermesTuiRpcError(
                    "Hermes session.activate returned no session key"
                )
            if visibility := _stored_delivery_visibility(
                url,
                _token,
                session_key=session_key,
                delivery_id=delivery_id,
                timeout_s=timeout_s,
                urlopen_fn=urlopen_fn,
            ):
                return HermesVisibleTurnReceipt(
                    status="already_visible",
                    visibility=visibility,
                    session_id=session_id,
                )
            result = _rpc(
                socket,
                3,
                "prompt.submit",
                {"session_id": session_id, "text": text},
            )
            status = str(result.get("status") or "").strip()
            if status not in {"streaming", "steered", "queued", "redirected"}:
                raise HermesTuiRpcError(
                    f"Hermes prompt.submit was not accepted: {result!r}"
                )
            for attempt in range(max_observations):
                observed = _rpc(
                    socket,
                    4 + attempt,
                    "session.activate",
                    {"session_id": session_id, "omit_messages": True},
                )
                if visibility := _delivery_visibility(observed, delivery_id):
                    return HermesVisibleTurnReceipt(
                        status=status,
                        visibility=visibility,
                        session_id=session_id,
                    )
                if poll_s > 0 and attempt + 1 < max_observations:
                    sleep_fn(poll_s)
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    raise HermesTuiRpcError(
        "Hermes prompt.submit was accepted "
        f"(status={status}) but delivery {delivery_id!r} was not visible in "
        "session messages, inflight input, or the native queue"
    )


__all__ = [
    "HermesTuiRpcError",
    "HermesTurnActivity",
    "HermesVisibleTurnReceipt",
    "_stored_delivery_visibility",
    "active_sessions",
    "gateway_detailed_health",
    "observe_turn_activity",
    "pause_heartbeat",
    "submit_turn",
    "submit_visible_turn",
]
