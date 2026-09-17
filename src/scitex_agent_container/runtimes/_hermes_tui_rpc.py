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
    delivery_mode: str = "steer"


@dataclass(frozen=True)
class HermesTurnReceipt:
    """Hermes-owned admission result for one interactive inbound message."""

    status: str
    delivery_mode: str
    session_id: str


@dataclass(frozen=True)
class HermesTurnProgress:
    """Monotonic live-session evidence used to verify stale recovery."""

    message_count: int
    last_active: float
    status: str


@dataclass(frozen=True)
class HermesTurnOutcome:
    """Authoritative terminal event after a recovery control operation."""

    progress: HermesTurnProgress
    terminal_status: str | None
    latest_seq: int
    epoch: str


@dataclass(frozen=True)
class HermesSlashReceipt:
    """A slash command result plus the post-command live-session baseline."""

    output: str
    session_id: str
    progress: HermesTurnProgress
    latest_seq: int
    epoch: str


@dataclass(frozen=True)
class HermesCompressionReceipt:
    """Verified before/after telemetry from native Hermes compression."""

    session_id: str
    before_tokens: int
    after_tokens: int
    before_messages: int
    after_messages: int
    context_used: int
    context_max: int
    context_source: str
    compressions: int


_SEARCH_RESPONSE_MAX_BYTES = 256 * 1024


def _detailed_health(
    port: int,
    token: str,
    *,
    timeout_s: float = 2.0,
    urlopen_fn: Any = urlopen,
) -> dict[str, Any]:
    """Exercise Hermes' authenticated, storage-backed readiness route.

    ``hermes serve`` is the TUI/dashboard backend.  Its public liveness route
    is ``/api/health``; ``/health/detailed`` belongs to the separate platform
    API gateway and is hidden by the headless backend's catch-all.  Hermes'
    own dashboard self-test uses ``/api/sessions?limit=1`` because it verifies
    both session-token authentication and a cheap state-database read.  Use
    that same contract here and normalize its response for SAC callers.
    """
    request = Request(
        f"http://127.0.0.1:{port}/api/sessions?limit=1",
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
    if (
        status != 200
        or not isinstance(payload, dict)
        or not isinstance(payload.get("sessions"), list)
    ):
        raise HermesTuiRpcError(
            "Hermes authenticated readiness returned a malformed response"
        )
    return {
        "status": "ok",
        "readiness": {"authenticated_session_store": "ok"},
    }


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


_FORK_RPC_MAX_BYTES = 64 * 1024 * 1024


def _connect(
    url: str,
    timeout_s: float,
    connect_fn: Any | None,
    *,
    max_size: int | None = None,
) -> Any:
    if connect_fn is None:
        try:
            from websockets.sync.client import connect as connect_fn
        except ImportError as exc:
            raise HermesTuiRpcError(
                "websockets>=15 is required for Hermes TUI delivery"
            ) from exc
    kwargs: dict[str, Any] = {"open_timeout": timeout_s, "close_timeout": 1}
    if max_size is not None:
        kwargs["max_size"] = max_size
    return connect_fn(url, **kwargs)


def branch_visible_history(
    state_dir: Path,
    *,
    parent_session_key: str,
    child_session_key: str,
    child_cwd: str,
    timeout_s: float = 30.0,
    connect_fn: Any | None = None,
) -> dict[str, Any]:
    """Fork one live parent through Hermes and return a portable visible seed.

    ``session.branch`` is the sole context-copy operation.  Its live branch and
    durable row are temporary extraction artifacts: close/delete exactly those
    returned identities before handing the transcript to the child profile.
    """
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(
            url,
            timeout_s,
            connect_fn,
            max_size=_FORK_RPC_MAX_BYTES,
        ) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            parent = _select_session_row(listing.get("sessions"), parent_session_key)
            expected_parent_stored = str(
                parent.get("stored_session_id")
                or parent.get("session_key")
                or parent.get("id")
                or ""
            ).strip()
            branch = _rpc(
                socket,
                2,
                "session.branch",
                {"session_id": str(parent["id"]), "name": child_session_key},
            )
            live_id = str(branch.get("session_id") or "").strip()
            stored_id = str(branch.get("stored_session_id") or "").strip()
            cleanup_errors: list[str] = []
            try:
                messages = branch.get("messages")
                count = branch.get("message_count")
                parent_id = str(branch.get("parent") or "").strip()
                if (
                    not live_id
                    or not stored_id
                    or branch.get("title") != child_session_key
                    or not parent_id
                    or parent_id != expected_parent_stored
                    or not isinstance(messages, list)
                    or not all(isinstance(message, dict) for message in messages)
                    or type(count) is not int
                    or count != len(messages)
                    or count < 1
                ):
                    raise HermesTuiRpcError(
                        "Hermes session.branch returned malformed fork history"
                    )
                seed = {
                    "version": 1,
                    "title": child_session_key,
                    "parent_session_id": parent_id,
                    "cwd": child_cwd,
                    "messages": messages,
                }
            finally:
                if live_id:
                    try:
                        closed = _rpc(
                            socket, 3, "session.close", {"session_id": live_id}
                        )
                        if closed.get("closed") is not True:
                            cleanup_errors.append("temporary live branch did not close")
                    except Exception:  # cleanup continues with the durable row
                        cleanup_errors.append("temporary live branch close failed")
                if stored_id:
                    try:
                        deleted = _rpc(
                            socket, 4, "session.delete", {"session_id": stored_id}
                        )
                        if deleted.get("deleted") != stored_id:
                            cleanup_errors.append(
                                "temporary stored branch did not delete"
                            )
                    except Exception:
                        cleanup_errors.append("temporary stored branch delete failed")
            if cleanup_errors:
                raise HermesTuiRpcError(
                    "Hermes fork cleanup failed: " + "; ".join(cleanup_errors)
                )
            return seed
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc


def _visible_text_projection(messages: object) -> list[tuple[str, str]]:
    if not isinstance(messages, list):
        return []
    projected: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = (
            message.get("text")
            if message.get("content") is None
            else message.get("content")
        )
        if role in {"user", "assistant", "system"} and isinstance(content, str):
            projected.append((role, content))
    return projected


def _one_stored_session(result: dict, title: str) -> dict | None:
    rows = result.get("sessions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HermesTuiRpcError("Hermes session.list returned malformed result")
    if len(rows) > 1:
        raise HermesTuiRpcError(
            f"Hermes session title {title!r} is ambiguous ({len(rows)} matches)"
        )
    return rows[0] if rows else None


def import_fork_seed(
    state_dir: Path,
    seed: dict[str, Any],
    *,
    timeout_s: float = 30.0,
    connect_fn: Any | None = None,
) -> str:
    """Import a portable fork seed through only native Hermes session RPCs.

    A cross-profile ``parent_session_id`` cannot be passed directly to
    ``session.create``: pinned Hermes enforces a foreign key to a row in the
    *child* state.db.  Create a hidden local seed parent from the exact visible
    history, then ``session.branch`` it to the requested child title.  This
    preserves native lineage without copying a database or weakening its FK.
    """
    if not isinstance(seed, dict) or seed.get("version") != 1:
        raise HermesTuiRpcError("Hermes fork seed has an unsupported format")
    title = str(seed.get("title") or "").strip()
    parent_id = str(seed.get("parent_session_id") or "").strip()
    parent_name = str(seed.get("parent_name") or "").strip()
    parent_engine = str(seed.get("parent_engine") or "").strip()
    cwd = str(seed.get("cwd") or "").strip()
    messages = seed.get("messages")
    from .._lifecycle._twin import _visible_history_digest

    try:
        digest = _visible_history_digest(messages)
    except Exception as exc:
        raise HermesTuiRpcError(f"Hermes fork seed is incomplete: {exc}") from exc
    if (
        not title
        or not parent_id
        or not parent_name
        or not parent_engine
        or not cwd
        or not isinstance(messages, list)
        or not messages
        or not all(isinstance(message, dict) for message in messages)
        or seed.get("visible_history_sha256") != digest
    ):
        raise HermesTuiRpcError("Hermes fork seed is incomplete")
    seed_title = f"{title} [fork seed:{parent_id}]"
    expected_projection = _visible_text_projection(messages)
    if len(expected_projection) != len(messages):
        raise HermesTuiRpcError("Hermes fork seed contains a non-visible message")
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(
            url,
            timeout_s,
            connect_fn,
            max_size=_FORK_RPC_MAX_BYTES,
        ) as socket:
            existing = _one_stored_session(
                _rpc(socket, 1, "session.list", {"title": title}), title
            )
            if existing is not None:
                if existing.get("message_count") != len(messages):
                    raise HermesTuiRpcError(
                        "existing Hermes fork has a different visible history size"
                    )
                stored_id = str(existing.get("id") or "").strip()
                if not stored_id:
                    raise HermesTuiRpcError("existing Hermes fork has no stored id")
                resumed = _rpc(socket, 2, "session.resume", {"session_id": stored_id})
                live_id = str(resumed.get("session_id") or "").strip()
                if (
                    not live_id
                    or resumed.get("message_count") != len(messages)
                    or _visible_text_projection(resumed.get("messages"))
                    != expected_projection
                ):
                    raise HermesTuiRpcError(
                        "existing Hermes fork does not match the pending seed"
                    )
                closed = _rpc(socket, 3, "session.close", {"session_id": live_id})
                if closed.get("closed") is not True:
                    raise HermesTuiRpcError(
                        "existing Hermes fork verification session did not close"
                    )
                return stored_id

            seed_parent = _one_stored_session(
                _rpc(socket, 2, "session.list", {"title": seed_title}), seed_title
            )
            if seed_parent is None:
                imported = _rpc(
                    socket,
                    3,
                    "session.create",
                    {
                        "messages": messages,
                        "title": seed_title,
                        "cwd": cwd,
                        "hidden": True,
                    },
                )
            else:
                imported = _rpc(
                    socket,
                    3,
                    "session.resume",
                    {"session_id": str(seed_parent.get("id") or "")},
                )
            seed_live_id = str(imported.get("session_id") or "").strip()
            seed_stored_id = str(
                imported.get("stored_session_id")
                or imported.get("session_key")
                or (seed_parent.get("id") if seed_parent else "")
            ).strip()
            if (
                not seed_live_id
                or not seed_stored_id
                or imported.get("message_count") != len(messages)
                or _visible_text_projection(imported.get("messages"))
                != expected_projection
            ):
                raise HermesTuiRpcError(
                    "Hermes session.create did not preserve the fork seed history"
                )

            branch: dict[str, Any] | None = None
            branch_live_id = ""
            cleanup_errors: list[str] = []
            try:
                branch = _rpc(
                    socket,
                    4,
                    "session.branch",
                    {"session_id": seed_live_id, "name": title},
                )
                branch_live_id = str(branch.get("session_id") or "").strip()
                child_stored_id = str(branch.get("stored_session_id") or "").strip()
                if (
                    not branch_live_id
                    or not child_stored_id
                    or branch.get("title") != title
                    or branch.get("parent") != seed_stored_id
                    or branch.get("message_count") != len(messages)
                    or _visible_text_projection(branch.get("messages"))
                    != expected_projection
                ):
                    raise HermesTuiRpcError(
                        "Hermes session.branch did not preserve the imported history"
                    )
            finally:
                request_id = 5
                if branch_live_id:
                    try:
                        closed = _rpc(
                            socket,
                            request_id,
                            "session.close",
                            {"session_id": branch_live_id},
                        )
                        if closed.get("closed") is not True:
                            cleanup_errors.append(
                                "imported child session did not close"
                            )
                    except Exception:
                        cleanup_errors.append("imported child session close failed")
                    request_id += 1
                if seed_live_id:
                    try:
                        closed = _rpc(
                            socket,
                            request_id,
                            "session.close",
                            {"session_id": seed_live_id},
                        )
                        if closed.get("closed") is not True:
                            cleanup_errors.append("seed parent session did not close")
                    except Exception:
                        cleanup_errors.append("seed parent session close failed")
            if cleanup_errors:
                raise HermesTuiRpcError(
                    "Hermes fork import cleanup failed: " + "; ".join(cleanup_errors)
                )
            return child_stored_id
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc


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
    if len(exact) != 1:
        raise HermesTuiRpcError(
            f"cannot identify one live Hermes session for {expected_title!r}: "
            f"{len(exact)} exact matches among {len(sessions)} live sessions"
        )
    row = exact[0]
    session_id = str(row.get("id") or "").strip()
    if not session_id:
        raise HermesTuiRpcError("Hermes active session has no id")
    return row


def _select_session(rows: object, expected_title: str) -> str:
    return str(_select_session_row(rows, expected_title)["id"])


def _turn_progress(row: dict) -> HermesTurnProgress:
    message_count = row.get("message_count")
    last_active = row.get("last_active")
    status = str(row.get("status") or "").strip().lower()
    if (
        type(message_count) is not int
        or message_count < 0
        or not isinstance(last_active, (int, float))
        or last_active < 0
        or status
        not in {"idle", "working", "waiting", "starting", "streaming", "resuming"}
    ):
        raise HermesTuiRpcError(
            f"Hermes active session returned malformed progress: {row!r}"
        )
    return HermesTurnProgress(message_count, float(last_active), status)


def observe_turn_progress(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> HermesTurnProgress:
    """Read monotonic completion evidence without attaching a new viewer."""
    rows = active_sessions(state_dir, timeout_s=timeout_s, connect_fn=connect_fn)
    return _turn_progress(_select_session_row(rows, f"sac:{agent_name}"))


def observe_turn_outcome(
    state_dir: Path,
    agent_name: str,
    *,
    after_seq: int,
    expected_epoch: str,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> HermesTurnOutcome:
    """Read the typed terminal event after a recovery watermark.

    ``session.activate`` is intentionally not used: activation rebinds the live
    renderer and touches ``last_active``, so a passive recovery observer would
    manufacture the progress it was trying to verify.  Hermes' replay ring is
    the non-mutating control-plane record of ``message.complete``.
    """
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            row = _select_session_row(listing.get("sessions"), f"sac:{agent_name}")
            progress = _turn_progress(row)
            replay = _rpc(
                socket,
                2,
                "session.events.since",
                {"session_id": str(row["id"]), "last_seen": after_seq},
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    events = replay.get("events")
    latest_seq = replay.get("latest_seq")
    epoch = replay.get("epoch")
    if (
        not isinstance(events, list)
        or type(latest_seq) is not int
        or latest_seq < after_seq
        or not isinstance(epoch, str)
        or not epoch
    ):
        raise HermesTuiRpcError(
            f"Hermes session.events.since returned malformed result: {replay!r}"
        )
    if epoch != expected_epoch:
        raise HermesTuiRpcError(
            "Hermes event replay epoch changed during stale recovery"
        )
    terminal_status = None
    for event in events:
        if not isinstance(event, dict):
            raise HermesTuiRpcError(
                f"Hermes event replay contained malformed event: {event!r}"
            )
        if event.get("type") != "message.complete":
            continue
        payload = event.get("payload")
        status = payload.get("status") if isinstance(payload, dict) else None
        if status not in {"complete", "error", "interrupted"}:
            raise HermesTuiRpcError(
                f"Hermes message.complete had malformed status: {event!r}"
            )
        terminal_status = str(status)
    return HermesTurnOutcome(progress, terminal_status, latest_seq, epoch)


def _session_activity(row: dict) -> str:
    """Normalize the gateway's authoritative activity without guessing."""
    status = str(row.get("status") or "").strip().lower()
    if status == "idle":
        return "idle"
    if status in {"working", "waiting", "starting"}:
        return "active"
    raise HermesTuiRpcError(
        f"Hermes session {row.get('id')!r} returned unknown activity status {status!r}"
    )


def _submit_interactive(
    socket: Any,
    *,
    session: dict,
    text: str,
    delivery_mode: str,
    request_id: int,
) -> tuple[HermesTurnReceipt, int]:
    """Use Hermes' intent-level steer or queue operation for this activity state."""
    if delivery_mode not in {"steer", "queue"}:
        raise ValueError("delivery_mode must be 'steer' or 'queue'")
    session_id = str(session["id"])
    activity = _session_activity(session)
    if delivery_mode == "steer" and activity == "active":
        result = _rpc(
            socket,
            request_id,
            "session.steer",
            {
                "render_user_message": True,
                "session_id": session_id,
                "text": text,
            },
        )
        # Hermes 0.21.1 calls its accepted pending-steer slot ``queued``.
        # This is distinct from its next-turn prompt queue: the method name is
        # the semantic contract and the echoed text binds the receipt.
        if result.get("status") != "queued" or result.get("text") != text:
            raise HermesTuiRpcError(
                f"Hermes session.steer did not accept the active-turn steer: {result!r}"
            )
        return HermesTurnReceipt("steered", "steer", session_id), request_id + 1

    params: dict[str, Any] = {
        "render_user_message": True,
        "session_id": session_id,
        "text": text,
    }
    if delivery_mode == "queue":
        params["queued"] = True
    result = _rpc(socket, request_id, "prompt.submit", params)
    status = str(result.get("status") or "").strip()
    allowed = (
        {"streaming", "queued"}
        if delivery_mode == "queue"
        else {
            "streaming",
            "steered",
        }
    )
    if status not in allowed:
        raise HermesTuiRpcError(
            f"Hermes {delivery_mode} delivery returned {result!r}; "
            "refusing to treat a different delivery semantic as success"
        )
    return HermesTurnReceipt(status, delivery_mode, session_id), request_id + 1


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
    session_id = str(row.get("id") or "").strip()
    status = str(row.get("status") or "").strip().lower()
    state = _session_activity(row)
    return HermesTurnActivity(state=state, session_status=status, session_id=session_id)


def submit_turn(
    state_dir: Path,
    agent_name: str,
    text: str,
    *,
    timeout_s: float = 10.0,
    delivery_mode: str = "steer",
    connect_fn: Any | None = None,
) -> HermesTurnReceipt:
    """Steer active work by default; queue only when explicitly requested."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session = _select_session_row(listing.get("sessions"), f"sac:{agent_name}")
            receipt, _next_id = _submit_interactive(
                socket,
                session=session,
                text=text,
                delivery_mode=delivery_mode,
                request_id=2,
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url} is unreachable: {exc}"
        ) from exc
    return receipt


def execute_slash_command(
    state_dir: Path,
    agent_name: str,
    command: str,
    *,
    timeout_s: float = 30.0,
    connect_fn: Any | None = None,
) -> HermesSlashReceipt:
    """Execute a slash command through Hermes' command dispatcher.

    Slash commands are control-plane operations, not conversational turns.
    Sending ``/model`` through ``prompt.submit`` records ordinary user text and
    never invokes Hermes' model-switch handler.  ``slash.exec`` runs the
    supported command path and mirrors its side effects onto the live session.
    """
    command = str(command or "").strip()
    if not command.startswith("/"):
        raise ValueError("Hermes slash command must start with '/'")
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session_id = _select_session(listing.get("sessions"), f"sac:{agent_name}")
            result = _rpc(
                socket,
                2,
                "slash.exec",
                {"session_id": session_id, "command": command},
            )
            after = _rpc(socket, 3, "session.active_list", {})
            progress = _turn_progress(
                _select_session_row(after.get("sessions"), f"sac:{agent_name}")
            )
            replay = _rpc(
                socket,
                4,
                "session.events.since",
                {"session_id": session_id, "last_seen": 0},
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url} is unreachable: {exc}"
        ) from exc
    output = result.get("output")
    if not isinstance(output, str) or not output.strip():
        raise HermesTuiRpcError(
            f"Hermes slash.exec returned malformed result: {result!r}"
        )
    if warning := result.get("warning"):
        raise HermesTuiRpcError(
            f"Hermes slash.exec did not synchronize the live session: {warning}"
        )
    latest_seq = replay.get("latest_seq")
    epoch = replay.get("epoch")
    if (
        type(latest_seq) is not int
        or latest_seq < 0
        or not isinstance(epoch, str)
        or not epoch
    ):
        raise HermesTuiRpcError(
            f"Hermes event replay returned malformed watermark: {replay!r}"
        )
    return HermesSlashReceipt(output, session_id, progress, latest_seq, epoch)


def clear_heartbeat_for_session(
    state_dir: Path,
    session_id: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> str:
    """Remove one exact session's heartbeat without creating a model turn.

    ``prompt.submit('/heartbeat clear')`` does *not* execute a slash command.
    It appends an ordinary user message and therefore wakes the model with the
    complete conversation.  Hermes exposes heartbeat state through its
    intent-level ``session.control`` RPC.  Clearing, rather than merely
    pausing, also prevents a persisted legacy heartbeat from being resumed by
    a later client or provider recovery.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        raise HermesTuiRpcError("Hermes heartbeat clear requires a session id")
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            result = _rpc(
                socket,
                1,
                "session.control",
                {"session_id": session_id, "action": "heartbeat.clear"},
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
    raise HermesTuiRpcError(f"Hermes heartbeat did not clear: {heartbeat!r}")


def clear_heartbeat(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 10.0,
    connect_fn: Any | None = None,
) -> str:
    """Resolve SAC's exact live session and remove its periodic wakeup."""
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session_id = _select_session(listing.get("sessions"), f"sac:{agent_name}")
            result = _rpc(
                socket,
                2,
                "session.control",
                {"session_id": session_id, "action": "heartbeat.clear"},
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
    if heartbeat is not None:
        raise HermesTuiRpcError(f"Hermes heartbeat did not clear: {heartbeat!r}")
    return "absent"


def _required_nonnegative_int(payload: dict, key: str, *, source: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise HermesTuiRpcError(f"Hermes {source} returned invalid {key}: {value!r}")
    return value


def compress_session(
    state_dir: Path,
    agent_name: str,
    *,
    timeout_s: float = 120.0,
    connect_fn: Any | None = None,
) -> HermesCompressionReceipt:
    """Compact one idle live session through Hermes' native control plane.

    This deliberately calls ``session.compress`` rather than submitting the
    text ``/compress`` as a model prompt. The native result supplies the
    before/after counts, while its post-commit ``info.usage`` projection proves
    the resulting provider-derived context state.
    """
    url, _token = _gateway_connection(state_dir)
    try:
        with _connect(url, timeout_s, connect_fn) as socket:
            listing = _rpc(socket, 1, "session.active_list", {})
            session = _select_session_row(listing.get("sessions"), f"sac:{agent_name}")
            if _session_activity(session) != "idle":
                raise HermesTuiRpcError(
                    f"Hermes session {session['id']!r} is busy; refusing compression"
                )
            session_id = str(session["id"])
            result = _rpc(
                socket,
                2,
                "session.compress",
                {"session_id": session_id},
            )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc

    if result.get("status") != "compressed":
        raise HermesTuiRpcError(
            f"Hermes session.compress did not commit compression: {result!r}"
        )
    before_tokens = _required_nonnegative_int(
        result, "before_tokens", source="session.compress"
    )
    after_tokens = _required_nonnegative_int(
        result, "after_tokens", source="session.compress"
    )
    before_messages = _required_nonnegative_int(
        result, "before_messages", source="session.compress"
    )
    after_messages = _required_nonnegative_int(
        result, "after_messages", source="session.compress"
    )
    if after_tokens >= before_tokens or after_messages >= before_messages:
        raise HermesTuiRpcError(
            "Hermes session.compress reported no strict context reduction"
        )
    info = result.get("info")
    usage = info.get("usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        raise HermesTuiRpcError(
            "Hermes session.compress returned no post-compression usage telemetry"
        )
    context_used = _required_nonnegative_int(
        usage, "context_used", source="session.compress info.usage"
    )
    context_max = _required_nonnegative_int(
        usage, "context_max", source="session.compress info.usage"
    )
    compressions = _required_nonnegative_int(
        usage, "compressions", source="session.compress info.usage"
    )
    context_source = str(usage.get("context_source") or "").strip()
    # Pinned Hermes increments ContextCompressor.compression_count while
    # finalizing the compressed message list, before session.compress builds
    # this post-commit info projection. A committed response therefore cannot
    # truthfully report zero compressions, even for the first manual pass.
    if (
        context_max <= 0
        or context_used > context_max
        or compressions <= 0
        or not context_source
    ):
        raise HermesTuiRpcError(
            "Hermes session.compress returned incomplete context telemetry"
        )
    return HermesCompressionReceipt(
        session_id=session_id,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        before_messages=before_messages,
        after_messages=after_messages,
        context_used=context_used,
        context_max=context_max,
        context_source=context_source,
        compressions=compressions,
    )


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
    delivery_mode: str = "steer",
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
            session = _select_session_row(listing.get("sessions"), f"sac:{agent_name}")
            session_id = str(session["id"])
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
                    delivery_mode=delivery_mode,
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
                    delivery_mode=delivery_mode,
                )
            receipt, next_request_id = _submit_interactive(
                socket,
                session=session,
                text=text,
                delivery_mode=delivery_mode,
                request_id=3,
            )
            for attempt in range(max_observations):
                observed = _rpc(
                    socket,
                    next_request_id + attempt,
                    "session.activate",
                    {"session_id": session_id, "omit_messages": True},
                )
                if visibility := _delivery_visibility(observed, delivery_id):
                    return HermesVisibleTurnReceipt(
                        status=receipt.status,
                        visibility=visibility,
                        session_id=session_id,
                        delivery_mode=delivery_mode,
                    )
                if poll_s > 0 and attempt + 1 < max_observations:
                    sleep_fn(poll_s)
            # ``prompt.submit`` can win the race with Hermes' lightweight
            # live projection: the input is committed just after the final
            # ``session.activate`` snapshot.  A caller that interprets that
            # visibility miss as non-delivery may then activate an independent
            # fallback rail and submit the same durable message twice.  Close
            # the observation window with the indexed persisted projection,
            # using the same upstream delivery identity checked before submit.
            # This is an observation only; never resubmit here.
            if visibility := _stored_delivery_visibility(
                url,
                _token,
                session_key=session_key,
                delivery_id=delivery_id,
                timeout_s=timeout_s,
                urlopen_fn=urlopen_fn,
            ):
                return HermesVisibleTurnReceipt(
                    status=receipt.status,
                    visibility=visibility,
                    session_id=session_id,
                    delivery_mode=delivery_mode,
                )
    except HermesTuiRpcError:
        raise
    except Exception as exc:
        raise HermesTuiRpcError(
            f"Hermes TUI gateway at {url.split('?')[0]} is unreachable: {exc}"
        ) from exc
    raise HermesTuiRpcError(
        "Hermes prompt.submit was accepted "
        f"(status={receipt.status}) but delivery {delivery_id!r} was not visible in "
        "session messages, inflight input, or the native queue"
    )


__all__ = [
    "HermesTuiRpcError",
    "HermesCompressionReceipt",
    "HermesTurnActivity",
    "HermesTurnReceipt",
    "HermesVisibleTurnReceipt",
    "_stored_delivery_visibility",
    "active_sessions",
    "branch_visible_history",
    "compress_session",
    "gateway_detailed_health",
    "import_fork_seed",
    "observe_turn_activity",
    "clear_heartbeat_for_session",
    "clear_heartbeat",
    "submit_turn",
    "submit_visible_turn",
]
