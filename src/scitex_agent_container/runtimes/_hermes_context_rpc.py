"""Hermes control-plane RPCs used only by deterministic context lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from ._hermes_tui_rpc import HermesTuiRpcError, _connect, _gateway_connection, _rpc

_SESSION_RESPONSE_MAX_BYTES = 256 * 1024


def stored_session_for_title(
    state_dir: Path,
    title: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
    urlopen_fn: Any = urlopen,
) -> dict[str, Any] | None:
    """Read one exact stored Hermes row through its authenticated APIs."""
    title = str(title or "").strip()
    if not title:
        raise ValueError("stored Hermes session lookup requires a title")
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(
                socket,
                1,
                "session.list",
                {"title": title, "include_hidden": True},
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    rows = listing.get("sessions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HermesTuiRpcError(
            f"Hermes session.list returned malformed result: {listing!r}"
        )
    if not rows:
        return None
    if len(rows) != 1:
        raise HermesTuiRpcError(
            f"Hermes session.list returned {len(rows)} exact rows for {title!r}"
        )
    stored_id = str(rows[0].get("resolved_id") or rows[0].get("id") or "").strip()
    if not stored_id:
        raise HermesTuiRpcError("Hermes stored session row has no id")
    return stored_session_record(
        state_dir,
        stored_id,
        timeout_s=timeout_s,
        urlopen_fn=urlopen_fn,
    )


def stored_session_for_identity(
    state_dir: Path,
    identity: str,
    mode: str,
) -> dict[str, Any] | None:
    """Resolve an age-gate record for a named continuation or explicit resume."""
    if mode == "resume":
        return stored_session_record(state_dir, identity)
    return stored_session_for_title(state_dir, identity)


def stored_session_record(
    state_dir: Path,
    stored_id: str,
    *,
    timeout_s: float = 10.0,
    urlopen_fn: Any = urlopen,
) -> dict[str, Any]:
    """Read one exact row from Hermes' authenticated session-management API."""
    stored_id = str(stored_id or "").strip()
    if not stored_id:
        raise ValueError("stored Hermes session record requires an id")
    url, token = _gateway_connection(state_dir)
    endpoint = urlsplit(url)
    request = Request(
        f"http://{endpoint.hostname}:{endpoint.port}/api/sessions/"
        f"{quote(stored_id, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen_fn(request, timeout=timeout_s) as response:
            encoded = response.read(_SESSION_RESPONSE_MAX_BYTES + 1)
            status = int(response.status)
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes stored session record is unavailable: {exc}"
        ) from exc
    if status != 200 or len(encoded) > _SESSION_RESPONSE_MAX_BYTES:
        raise HermesTuiRpcError("Hermes stored session record is unreadable or too large")
    try:
        record = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise HermesTuiRpcError(
            f"Hermes stored session record returned malformed JSON: {exc}"
        ) from exc
    if not isinstance(record, dict) or record.get("id") != stored_id:
        raise HermesTuiRpcError("Hermes stored session record identity is malformed")
    return record


def session_handoff_facts(
    state_dir: Path,
    session_id: str,
    *,
    workdir: Path,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> tuple[dict, list[dict]]:
    """Return Hermes verification evidence and active subagent records."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            verification = _rpc(
                socket,
                1,
                "verification.status",
                {"session_id": session_id, "cwd": str(workdir)},
            )
            delegation = _rpc(socket, 2, "delegation.status", {})
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes handoff facts at {url.split('?')[0]} are unavailable: {exc}"
        ) from exc
    active = delegation.get("active")
    if not isinstance(active, list) or not all(isinstance(row, dict) for row in active):
        raise HermesTuiRpcError(
            f"Hermes delegation.status returned malformed result: {delegation!r}"
        )
    evidence = verification.get("verification")
    if not isinstance(evidence, dict):
        raise HermesTuiRpcError(
            f"Hermes verification.status returned malformed result: {verification!r}"
        )
    return evidence, active


def close_session(
    state_dir: Path,
    session_id: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> None:
    """Close one exact live Hermes session and verify the acknowledgement."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            result = _rpc(socket, 1, "session.close", {"session_id": session_id})
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes session.close at {url.split('?')[0]} failed: {exc}"
        ) from exc
    if result.get("closed") is not True:
        raise HermesTuiRpcError(f"Hermes session.close was not acknowledged: {result!r}")


def _message_text(message: dict) -> str:
    value = message.get("content", message.get("text", ""))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "") for item in value if isinstance(item, dict)
        )
    return ""


def replace_session_from_handoff(
    state_dir: Path,
    *,
    agent_name: str,
    workdir: Path,
    handoff_path: Path,
    nonce: str,
    old_session_id: str,
    timeout_s: float = 120.0,
    max_observations: int = 60,
    poll_s: float = 1.0,
    connect_fn: Any | None = None,
    sleep_fn: Any = time.sleep,
    pre_close_check: Callable[[], None] | None = None,
) -> str:
    """Start and prove a fresh handoff reader before closing the old session."""
    nonce = str(nonce or "").strip()
    if not nonce:
        raise ValueError("fresh Hermes handoff requires a nonce")
    url, _token = _gateway_connection(state_dir)
    marker = f"HANDOFF_READY:{nonce}"
    prompt = (
        f"Read the durable handoff JSON at {handoff_path}. Treat GitHub, Cards, "
        f"and referenced artifacts as source of truth. Reply exactly {marker} "
        "before taking any other action."
    )
    fresh_live = ""
    old_closed = False
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            created = _rpc(
                socket,
                1,
                "session.create",
                {
                    "title": f"sac:{agent_name}:handoff:{nonce[:8]}",
                    "cwd": str(workdir),
                    "source": "cli",
                    "close_on_disconnect": False,
                },
            )
            fresh_live = str(created.get("session_id") or "").strip()
            fresh_stored = str(created.get("stored_session_id") or "").strip()
            if not fresh_live or not fresh_stored:
                raise HermesTuiRpcError(
                    f"Hermes session.create returned no fresh identity: {created!r}"
                )
            baseline = _rpc(
                socket,
                2,
                "session.events.since",
                {"session_id": fresh_live, "last_seen": 0},
            )
            latest_seq = baseline.get("latest_seq")
            epoch = baseline.get("epoch")
            if type(latest_seq) is not int or not isinstance(epoch, str) or not epoch:
                raise HermesTuiRpcError(
                    f"Hermes fresh-session replay baseline is malformed: {baseline!r}"
                )
            submitted = _rpc(
                socket,
                3,
                "prompt.submit",
                {"session_id": fresh_live, "text": prompt},
            )
            if submitted.get("status") not in {"streaming", "queued"}:
                raise HermesTuiRpcError(
                    f"Hermes fresh-session handoff was not admitted: {submitted!r}"
                )
            request_id = 4
            complete = False
            for attempt in range(max_observations):
                replay = _rpc(
                    socket,
                    request_id,
                    "session.events.since",
                    {"session_id": fresh_live, "last_seen": latest_seq},
                )
                request_id += 1
                if replay.get("epoch") != epoch:
                    raise HermesTuiRpcError(
                        "Hermes fresh-session replay epoch changed before nonce proof"
                    )
                current_seq = replay.get("latest_seq")
                events = replay.get("events")
                if type(current_seq) is not int or not isinstance(events, list):
                    raise HermesTuiRpcError(
                        f"Hermes fresh-session replay is malformed: {replay!r}"
                    )
                latest_seq = current_seq
                complete = any(
                    isinstance(event, dict)
                    and event.get("type") == "message.complete"
                    and isinstance(event.get("payload"), dict)
                    and event["payload"].get("status") == "complete"
                    for event in events
                )
                if complete:
                    break
                if poll_s > 0 and attempt + 1 < max_observations:
                    sleep_fn(poll_s)
            if not complete:
                raise HermesTuiRpcError(
                    "Hermes fresh session did not complete the handoff proof turn"
                )
            history = _rpc(
                socket,
                request_id,
                "session.history",
                {"session_id": fresh_live},
            )
            request_id += 1
            messages = history.get("messages")
            proven = isinstance(messages, list) and any(
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and marker in _message_text(message)
                for message in messages
            )
            if not proven:
                raise HermesTuiRpcError(
                    "Hermes fresh session completed without the required nonce proof"
                )
            if pre_close_check is not None:
                pre_close_check()
            closed = _rpc(
                socket,
                request_id,
                "session.close",
                {"session_id": old_session_id},
            )
            if closed.get("closed") is not True:
                raise HermesTuiRpcError(
                    f"Hermes old session did not close after nonce proof: {closed!r}"
                )
            old_closed = True
    except HermesTuiRpcError:
        if fresh_live and not old_closed:
            _close_failed_candidate(
                state_dir, fresh_live, timeout_s=timeout_s, connect_fn=connect_fn
            )
        raise
    except Exception as exc:
        if fresh_live and not old_closed:
            _close_failed_candidate(
                state_dir, fresh_live, timeout_s=timeout_s, connect_fn=connect_fn
            )
        raise HermesTuiRpcError(
            f"Hermes handoff rotation at {url.split('?')[0]} failed: {exc}"
        ) from exc
    return fresh_stored


def _close_failed_candidate(
    state_dir: Path,
    session_id: str,
    *,
    timeout_s: float,
    connect_fn: Any | None,
) -> None:
    """Fail closed if a rejected fresh candidate cannot be retired."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            result = _rpc(
                socket, 999, "session.close", {"session_id": session_id}
            )
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes fresh candidate cleanup at {url.split('?')[0]} failed: {exc}"
        ) from exc
    if result.get("closed") is not True:
        raise HermesTuiRpcError(
            f"Hermes fresh candidate cleanup was not acknowledged: {result!r}"
        )


__all__ = [
    "close_session",
    "replace_session_from_handoff",
    "session_handoff_facts",
    "stored_session_for_identity",
    "stored_session_for_title",
    "stored_session_record",
]
