"""Host-side A2A ``/v1/turn`` bridge for ``runtime: tui`` agents.

Closes the wake-on-push gap for interactive TUI agents. The SDK runtime
serves ``/v1/turn`` from its in-SIF runner so the ``sac mcp channel``
subscriber's wake POST (``_mcp/_channel_wake._wake_turn``) DRIVES an idle
agent to act; the TUI runtime runs ``claude`` in tmux with no in-process
HTTP server, so that POST hit a dead port and the message never woke it.
This module gives TUI agents the SAME endpoint host-side (the in-SIF
subscriber POSTs to ``127.0.0.1:<port>`` — apptainer shares the host net
namespace): on ``POST /v1/turn`` it first persists a durable exchange, then
asks the selected runtime to deliver ``text``. Hermes uses its native
shared-session JSON-RPC; legacy TUI adapters retain their runtime-specific
path. Identified durable callers receive ``202`` plus the exchange id and poll
the canonical ``scitex_dev.status`` ledger for the worker's final ``200``.
CCT's legacy bare ``{text}`` wake has no polling identity and treats every 2xx
as final, so the bridge returns ``200`` only after the selected TUI adapter's
admission proof (or a non-2xx response that keeps the Telegram item retryable).
A failed durable visibility attempt remains non-final ``102`` so the same
exchange can advance on retry; ordinary, non-durable turn failures are final
``502``.

Wire format mirrors ``_session_http`` so ``_wake_turn`` + A2A clients work
unchanged:

    POST /v1/turn                      (bare — the port identifies the agent)
    POST /agents/<name>/turn           (canonical sac namespace)
    POST /agents/<name>/send           (A2A v1 alias)
    Content-Type: application/json
    {"text": "...", "from_agent": "<peer>"?, "dispatch_id": "<id>"?}

    200/202 {"exchange_id": "xch_...", "status_code": {...}}
    400 {"error": "missing or empty 'text' field"}        # schema mismatch, loud
    404 {"error": "..."}                                  # unknown route / wrong agent
    503 {"error": "...", "status_code": {...}}            # ledger rejected persistence

Turn delivery never changes the already-returned HTTP response. Its separate
HTTP 200, retryable 102, or ordinary-turn 502 is read from
``GET /v1/exchanges/<id>``.

Lifecycle (``start_turn_bridge`` / ``stop_turn_bridge`` + helpers) lives in
:mod:`_tui_turn_bridge_lifecycle` (module line cap) and is re-exported here so
the public ``_tui_turn_bridge.start_turn_bridge`` / ``stop_turn_bridge`` /
``resolved_a2a_port`` surface is unchanged; :func:`start_turn_bridge` spawns
THIS module as ``python -m`` (see :func:`main`), and :func:`stop_turn_bridge`
SIGTERMs it, waits for the port to release, and force-kills any own-port
survivor (the restart port-collision fix). Both are best-effort — a failed
bridge must never block agent start/stop.

BIND ADDRESS: ``spec.a2a.host``, resolved by
:func:`_tui_turn_bridge_lifecycle.resolved_a2a_host` and threaded through the
launcher's ``--host`` into :func:`serve`. It defaults to ``127.0.0.1``
(loopback wake POST; the bind is the security boundary, matching the SDK
runner's unauthed endpoint), which is what every fleet spec declares today —
so an unmodified spec binds loopback exactly as before. A spec that names a
different address now MOVES this bind with it, instead of leaving the bridge
on loopback while ``a2a_sidecar`` alone honoured the declaration.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import scitex_logging as slogging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any, Callable

from scitex_dev.status import Check, StatusCode

from ..config import AgentConfig
from ._tui_turn_bridge_lifecycle import (
    DEFAULT_HOST,
    LOG_FILENAME,
    MODULE_PATH,
    PID_FILENAME,
    _pid_path,
    _state_dir,
    resolved_a2a_host,
    resolved_a2a_port,
    start_turn_bridge,
    stop_turn_bridge,
)
from ._tui_turn_bridge_port import (
    TurnBridgePortBusyError,
    port_busy_error,
    port_is_free,
)
from ._turn_exchange_ledger import (
    TurnExchangeStoreUnavailable,
    finish_turn_exchange,
    open_turn_exchange,
    preflight_turn_exchange_store,
    read_turn_exchange,
)

log = slogging.getLogger(__name__)

_CHANNEL_OPEN_RE = re.compile(r"^<channel\s+(?P<attrs>[^>]+)>")
_CHANNEL_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')
_CCT_SOURCE_ALIASES = frozenset({"cct", "claude-code-telegrammer"})


def _native_cause(exc: BaseException) -> StatusCode | None:
    """Preserve the first native errno through the turn HTTP boundary."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        number = getattr(current, "errno", None)
        name = errno.errorcode.get(number) if isinstance(number, int) else None
        if name:
            return StatusCode(kind="errno", code=name, message=str(current))
        nested = getattr(current, "__cause__", None)
        current = nested if isinstance(nested, BaseException) else None
    return None


def _admission_failure(exc: BaseException, *, agent: str) -> Check:
    """Describe a failed TUI-harness admission with its native cause."""
    return Check.not_ok(
        "tui_turn_admitted",
        f"the target TUI did not admit the turn for {agent!r}: {exc}",
        "Restore the resource named by `cause`, then retry the same durable "
        "delivery; its exchange identity makes admission idempotent.",
        cause=_native_cause(exc),
    )


def _cct_delivery_id(agent_name: str, text: str) -> str | None:
    """Derive one retry-stable identity from CCT's Telegram envelope.

    CCT v0.6.x posts only ``{"text": "<channel ...>..."}``, but its envelope
    includes Telegram's immutable ``chat_id`` + ``message_id`` pair. Use that
    pair rather than the message body: two separate, identical "hello"
    messages must remain two deliveries while a transport retry of one message
    must reopen the same exchange.
    """
    opening = _CHANNEL_OPEN_RE.match(text)
    if opening is None:
        return None
    attrs = dict(_CHANNEL_ATTR_RE.findall(opening.group("attrs")))
    # CCT authors ``source=cct`` on its wake rail. Claude Code renders the
    # same MCP notification under the server name ``claude-code-telegrammer``.
    # The transport label is not identity: normalize both spellings before
    # deriving the key so a rail change cannot turn one Telegram update into
    # two SAC operations.
    if attrs.get("source") not in _CCT_SOURCE_ALIASES:
        return None
    chat_id = attrs.get("chat_id", "").strip()
    message_id = attrs.get("message_id", "").strip()
    if not chat_id or not message_id:
        return None
    digest = hashlib.sha256(
        # Keep the historical namespace so identities authored before this
        # source-alias normalization remain stable across deployment.
        f"{agent_name}\0cct\0{chat_id}\0{message_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"cct_{digest}"


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------
def extract_turn_text(body: object) -> tuple[str | None, dict]:
    """Pull the turn text (and sac metadata) out of either accepted body shape.

    Returns ``(text, metadata)`` — ``metadata`` is the envelope's
    ``params.metadata`` when there is one, else ``{}``, so the caller can fall
    back to it for ``from_agent`` / ``dispatch_id``.

    TWO SHAPES REACH THIS BRIDGE, and accepting only one is what broke
    cross-host messaging on 2026-09-02 even after the route alias landed:

        flat      {"text": "...", "from_agent": "...", "dispatch_id": "..."}
        A2A v1    {"jsonrpc": "2.0", "method": "SendMessage", "params":
                   {"message": {"parts": [{"text": "..."}]},
                    "metadata": {"from_agent": ..., "dispatch_id": ...}}}

    The flat form is what ``sac listen`` synthesises for a local wake. The
    envelope is what every a2a caller in this package actually sends
    (``_channel_tools._wrap_message_send``) — sac extension fields live under
    ``params.metadata`` because A2A v1's strict validator rejects unknown
    fields at the params root. A bridge that reads only ``body["text"]``
    answers ``missing or empty 'text' field`` to a perfectly well-formed peer
    message, which is what it did.

    Multi-part messages are joined with newlines rather than silently taking
    part[0]: dropping the tail of a message is worse than a long inject.
    """
    if not isinstance(body, dict):
        return None, {}
    flat = body.get("text")
    if isinstance(flat, str) and flat.strip():
        return flat, {}
    params = body.get("params")
    if not isinstance(params, dict):
        return None, {}
    meta = params.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    message = params.get("message")
    if not isinstance(message, dict):
        return None, meta
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None, meta
    texts = [
        p["text"]
        for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()
    ]
    return ("\n".join(texts) if texts else None), meta


def is_turn_route(path: str, agent_name: str) -> bool:
    """True iff ``path`` is a turn-delivery route for ``agent_name``.

    Accepts the bare ``/v1/turn`` (the port already identifies the agent)
    and the named ``/agents/<agent_name>/{turn,send,message:send}`` aliases.
    A named route for a DIFFERENT agent is rejected (the caller returns 404)
    so a misrouted POST fails loud rather than landing in the wrong session.

    ``message:send`` IS THE FLEET'S A2A VERB, and omitting it here cost a
    live cross-host outage on 2026-09-02: every peer send to `figrecipe` on
    compute-03 died with ``no turn route
    '/agents/figrecipe/message:send'`` while the agent was healthy — running,
    registered, one live inbox subscriber. Nothing in that 404 says "wrong
    port": it reads as if the agent is missing, so the hour went to peer
    tokens, listen restarts and registry collisions before the path itself
    was read.

    The asymmetry that hid it: a peer resolving the target to the HOST's
    listen port reaches ``sac listen``, which serves ``message:send``
    (see ``_channel_tools`` / ``_session_completion`` — the same spelling
    everywhere); a peer resolving to the AGENT's own a2a port reaches this
    bridge instead, and only here was the verb unknown. Which of the two a
    peer resolves is a registry detail no caller controls, so the bridge
    must answer the same verb its listen does.
    """
    clean = path.split("?", 1)[0].rstrip("/")
    if clean == "/v1/turn":
        return True
    if agent_name and clean in (
        f"/agents/{agent_name}/turn",
        f"/agents/{agent_name}/send",
        f"/agents/{agent_name}/message:send",
    ):
        return True
    return False


def is_control_route(path: str, agent_name: str) -> bool:
    """True for the neutral SAC UI-control endpoint for this agent."""
    clean = path.split("?", 1)[0].rstrip("/")
    return clean == "/v1/control" or (
        bool(agent_name) and clean == f"/agents/{agent_name}/control"
    )


def _exchange_receipt(row: dict[str, Any]) -> dict[str, Any]:
    """Project the canonical status row into an explicit delivery receipt."""
    code = row.get("code")
    final = bool(row.get("final"))
    if not final:
        state = "retryable" if code == 102 else "pending"
    elif row.get("kind") == "http" and code == 200:
        state = "delivered"
    else:
        state = "failed"
    receipt: dict[str, Any] = {"state": state, "final": final}
    if state in {"retryable", "failed"}:
        receipt["error"] = str(row.get("message") or "delivery failed")
    return receipt


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class _TurnBridgeServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the agent name + inject callback.

    ``daemon_threads`` so a SIGTERM tears the server down without waiting
    on an in-flight inject thread (the keystrokes are already delivered;
    the driven turn lives in the TUI, not here).
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        on_turn: Callable[..., None],
        agent_name: str,
        on_control: Callable[[str], None] | None = None,
        *,
        exchange_open: Callable[..., tuple[str, str]] = open_turn_exchange,
        exchange_finish: Callable[..., None] = finish_turn_exchange,
        exchange_read: Callable[[str], dict[str, Any] | None] = read_turn_exchange,
    ) -> None:
        super().__init__(server_address, _TurnBridgeHandler)
        self.on_turn = on_turn
        self.agent_name = agent_name
        self.on_control = on_control
        self.exchange_open = exchange_open
        self.exchange_finish = exchange_finish
        self.exchange_read = exchange_read
        # Cards, SAC A2A and a human-triggered control request can arrive on
        # different HTTP threads. Native submit + session-projection proof is
        # one critical section; serializing it also makes the delivery-id
        # precheck and prompt.submit atomic relative to this bridge's callers.
        self.delivery_lock = threading.Lock()
        self.admission_lock = threading.Lock()
        self.active_delivery: tuple[str, str] | None = None


class _TurnBridgeHandler(BaseHTTPRequestHandler):
    """One route family: turn delivery + a health probe."""

    # Silence the default stderr access log — the bridge log file is for
    # our own diagnostics, not one line per loopback POST.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002,D401
        return

    def _srv(self) -> _TurnBridgeServer:
        # Real narrowing (PA-306 no-mocks): ``self.server`` IS a
        # _TurnBridgeServer at runtime; assert it so type-checkers see the
        # ``on_turn`` / ``agent_name`` attributes without a class-level
        # annotation override Pyright rejects as variance-incompatible.
        srv = self.server
        assert isinstance(srv, _TurnBridgeServer)
        return srv

    def _respond(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
        clean = self.path.split("?", 1)[0].rstrip("/")
        if clean == "/health":
            self._respond(200, {"status": "ok", "agent": self._srv().agent_name})
            return
        prefix = "/v1/exchanges/"
        if clean.startswith(prefix):
            exchange_id = clean[len(prefix) :]
            row = self._srv().exchange_read(exchange_id)
            if row is None:
                self._respond(404, {"error": f"unknown exchange {exchange_id!r}"})
                return
            self._respond(
                200,
                {
                    "exchange_id": exchange_id,
                    "receipt": _exchange_receipt(row),
                    "status_code": {
                        "kind": row["kind"],
                        "code": row["code"],
                        "message": row["message"],
                    },
                },
            )
            return
        self._respond(404, {"error": f"no GET route {self.path!r}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler contract)
        srv = self._srv()
        # Drain the request body FIRST (even on a route miss) so a 404
        # never leaves an unread body on the socket.
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        turn_route = is_turn_route(self.path, srv.agent_name)
        control_route = is_control_route(self.path, srv.agent_name)
        if not turn_route and not control_route:
            self._respond(404, {"error": f"no turn route {self.path!r}"})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            self._respond(400, {"error": f"bad JSON: {exc}"})
            return
        if control_route:
            key = body.get("key") if isinstance(body, dict) else None
            if key not in {"Enter", "Escape", "ESC", "C-c", "SIGINT"}:
                self._respond(
                    400, {"error": "control key must be Enter, Escape, or C-c"}
                )
                return
            if srv.on_control is None:
                self._respond(501, {"error": "this runtime has no UI-control surface"})
                return
            try:
                srv.on_control(str(key))
            except Exception as exc:
                self._respond(502, {"error": f"tui control failed: {exc}"})
                return
            self._respond(
                200,
                {
                    "delivered": True,
                    "mode": "tui-control",
                    "key": key,
                    "agent": srv.agent_name,
                },
            )
            return

        text, envelope_meta = extract_turn_text(body)
        if not isinstance(text, str) or not text.strip():
            self._respond(400, {"error": "missing or empty 'text' field"})
            return
        # Requester identity (optional) — the peer that dispatched this
        # wake. Threaded to on_turn so the inbound is recorded in the
        # ledger for the Stop-hook completion report (SDK parity). Absent
        # for an operator send / boot turn → no report is owed.
        raw_from = body.get("from_agent") if isinstance(body, dict) else None
        raw_did = body.get("dispatch_id") if isinstance(body, dict) else None
        # An A2A envelope carries these under ``params.metadata`` instead of at
        # the root; the flat form still wins when both are present.
        raw_from = raw_from or envelope_meta.get("from_agent")
        raw_did = raw_did or envelope_meta.get("dispatch_id")
        raw_mode = (
            body.get("delivery_mode") if isinstance(body, dict) else None
        ) or envelope_meta.get("delivery_mode")
        delivery_mode = str(raw_mode or "steer").strip().lower()
        if delivery_mode not in {"steer", "queue"}:
            self._respond(
                400,
                {"error": "delivery_mode must be 'steer' or 'queue'"},
            )
            return
        from_agent = raw_from if isinstance(raw_from, str) and raw_from else None
        dispatch_id = raw_did if isinstance(raw_did, str) and raw_did else None
        visible_delivery_id = body.get("visible_delivery_id")
        if not isinstance(visible_delivery_id, str) or not visible_delivery_id:
            visible_delivery_id = None
        if visible_delivery_id is not None and (
            f"<!-- delivery:{visible_delivery_id} -->" not in text
        ):
            self._respond(
                400,
                {
                    "error": (
                        "visible_delivery_id is not bound to the submitted text; "
                        "include its exact delivery marker before retrying"
                    )
                },
            )
            return
        requested_exchange_id = body.get("exchange_id")
        if not isinstance(requested_exchange_id, str) or not requested_exchange_id:
            requested_exchange_id = None
        # CCT v0.6.x supplies neither root identifier and treats any 2xx as a
        # final acknowledgement. Its channel envelope does carry Telegram's
        # immutable chat/message identity, so derive a stable opaque id from
        # that pair. Ordinary bare and A2A turns keep their asynchronous 202
        # contract; only a positively identified CCT wake uses the synchronous
        # native-visibility compatibility path below.
        cct_delivery_id = (
            _cct_delivery_id(srv.agent_name, text)
            if requested_exchange_id is None and visible_delivery_id is None
            else None
        )
        implicit_delivery = cct_delivery_id is not None
        effective_visible_id = visible_delivery_id
        if implicit_delivery:
            effective_visible_id = cct_delivery_id
        delivery_key = requested_exchange_id or effective_visible_id or ""
        with srv.admission_lock:
            active = srv.active_delivery
            if active is not None:
                active_key, active_exchange_id = active
                if delivery_key and delivery_key == active_key:
                    if implicit_delivery:
                        self._respond(
                            409,
                            {
                                "error": "this Telegram delivery is still in progress",
                                "agent": srv.agent_name,
                                "exchange_id": active_exchange_id,
                                "delivery_id": effective_visible_id,
                                "status_code": StatusCode(
                                    kind="http",
                                    code=409,
                                    message=(
                                        "target-harness admission is not final; leave "
                                        "the Telegram item unacknowledged and retry"
                                    ),
                                ).to_dict(),
                            },
                        )
                        return
                    self._respond(
                        202,
                        {
                            "exchange_id": active_exchange_id,
                            "status_code": StatusCode(
                                kind="http",
                                code=202,
                                message=(
                                    "this delivery is already in progress; poll "
                                    f"`/v1/exchanges/{active_exchange_id}`"
                                ),
                            ).to_dict(),
                        },
                    )
                    return
                self._respond(
                    429,
                    {
                        "error": "another turn delivery is already in progress",
                        "status_code": StatusCode(
                            kind="http",
                            code=429,
                            message=(
                                "one turn already owns this agent session; leave the "
                                "durable message unacknowledged and retry after polling "
                                f"`/v1/exchanges/{active_exchange_id}`"
                            ),
                        ).to_dict(),
                    },
                )
                return
            try:
                open_kwargs = {
                    "agent": srv.agent_name,
                    "probe_url": "/v1/exchanges",
                    "exchange_id": requested_exchange_id,
                    "delivery_id": effective_visible_id,
                }
                # Identity binding applies when adopting a Cards-owned
                # exchange. Keep the ordinary turn seam compatible with
                # external ledger adapters that don't consume initiator.
                if requested_exchange_id is not None:
                    open_kwargs["initiator"] = from_agent
                exchange_id, opened_at = srv.exchange_open(**open_kwargs)
            except Exception as exc:  # stx-allow: fallback (reason: without the canonical durable exchange row, 202 would claim an acceptance the responder cannot later answer for)
                log.exception(
                    "could not persist turn exchange for agent=%s", srv.agent_name
                )
                self._respond(
                    503,
                    {
                        "error": (
                            str(exc)
                            if isinstance(exc, TurnExchangeStoreUnavailable)
                            else "could not persist the canonical turn exchange"
                        ),
                        "status_code": StatusCode(
                            kind="http",
                            code=503,
                            message=(
                                "the canonical exchange ledger did not accept the turn; "
                                "leave the Cards notification unconfirmed and follow "
                                "the database-provisioning hint in `error` before retrying"
                            ),
                        ).to_dict(),
                    },
                )
                return
            existing = srv.exchange_read(exchange_id)
            if (
                isinstance(existing, dict)
                and existing.get("kind") == "http"
                and existing.get("code") == 200
            ):
                # The harness turn was already proven visible; the only
                # remaining work can be the caller's downstream Cards ACK.
                # Never depend on a terminal viewport and never submit the
                # same durable message twice.
                response_code = 200 if implicit_delivery else 202
                self._respond(
                    response_code,
                    {
                        "agent": srv.agent_name,
                        "exchange_id": exchange_id,
                        "delivery_id": effective_visible_id,
                        "status_code": StatusCode(
                            kind="http",
                            code=response_code,
                            message=(
                                "target-harness delivery is already final; "
                                + (
                                    "the Telegram wake is acknowledged without a "
                                    "duplicate prompt"
                                    if implicit_delivery
                                    else f"poll `/v1/exchanges/{exchange_id}` and retry "
                                    "only the downstream acknowledgement"
                                )
                            ),
                        ).to_dict(),
                    },
                )
                return
            # The production Hermes callback can prove an input in its native
            # transcript/inflight/queue projections.  Require that proof even
            # for an ordinary ``sac agents send`` whose caller did not supply
            # a Cards/CCT delivery id: the responder-issued exchange id is a
            # stable marker for this accepted operation.  Without this gate,
            # ``prompt.submit`` could acknowledge the RPC while the input
            # never appeared in the live session, and this bridge would forge
            # a final delivered/200 result.
            #
            # Generic callbacks retain their historical contract.  The
            # capability flag is attached only by ``_build_on_turn`` below,
            # whose runtime path actually implements ``send_visible_turn``.
            if effective_visible_id is None and getattr(
                srv.on_turn, "_sac_requires_visible_delivery", False
            ):
                effective_visible_id = exchange_id
            srv.active_delivery = (delivery_key or exchange_id, exchange_id)

        failure_check: Check | None = None
        native_visibility_required = bool(
            getattr(srv.on_turn, "_sac_requires_visible_delivery", False)
        )

        def deliver() -> StatusCode:
            nonlocal failure_check
            try:
                delivery_kwargs = {
                    "from_agent": from_agent,
                    "dispatch_id": dispatch_id,
                    "visible_delivery_id": effective_visible_id,
                    "delivery_mode": delivery_mode,
                }
                with srv.delivery_lock:
                    delivery = srv.on_turn(text, **delivery_kwargs)
                # Legacy/ordinary callbacks intentionally return ``None``
                # after a successful injection. Only visibility-gated
                # deliveries promise a native receipt object whose falsiness
                # means that the transcript proof failed.
                if (
                    native_visibility_required
                    and effective_visible_id
                    and not delivery
                ):
                    raise RuntimeError("Hermes transcript visibility was not confirmed")
                native_status = str(getattr(delivery, "status", "") or "")
                native_mode = str(
                    getattr(delivery, "delivery_mode", "") or delivery_mode
                )
                visibility = str(getattr(delivery, "visibility", "") or "")
                if native_status == "already_visible" and visibility:
                    visible_message = (
                        "Hermes delivery was already visible "
                        f"(status={native_status}, proof={visibility}); "
                        "no duplicate prompt was submitted"
                    )
                elif native_status and visibility:
                    visible_message = (
                        "Hermes prompt.submit accepted the visible turn "
                        f"(status={native_status}, proof={visibility})"
                    )
                elif native_status:
                    visible_message = (
                        "Hermes accepted the interactive inbound "
                        f"(mode={native_mode}, status={native_status})"
                    )
                elif native_visibility_required:
                    visible_message = (
                        "the incoming turn is visible in the Hermes transcript"
                    )
                else:
                    # Claude Code and Codex TUI adapters expose the established
                    # bool-returning pane-injection contract, not Hermes'
                    # structured transcript projection. `_build_on_turn`
                    # normalizes their successful True to None for backwards
                    # compatibility. A caller-supplied delivery marker must not
                    # silently opt those harnesses into Hermes-only proof and
                    # strand an exchange at retryable/102 after the prompt was
                    # visibly accepted and answered.
                    visible_message = "the target TUI accepted the identified turn"
                status = StatusCode(
                    kind="http",
                    code=200,
                    message=(
                        visible_message
                        if effective_visible_id
                        else "the TUI accepted the turn"
                    ),
                )
            except Exception as exc:  # stx-allow: fallback (reason: visibility failure must remain explicitly retryable on the accepted durable operation, never be acknowledged or rewritten from a terminal failure)
                detail = str(exc).strip() or "no native error detail"
                native_cause = _native_cause(exc)
                harness_name = (
                    "Hermes" if native_visibility_required else "TUI harness"
                )
                cause = native_cause or StatusCode(
                    kind="http",
                    code=502,
                    message=(
                        f"{harness_name} delivery failed "
                        f"({type(exc).__name__}: {detail}); no downstream "
                        "acknowledgement was issued"
                    ),
                )
                hint = (
                    "leave the Cards notification unconfirmed; inspect "
                    f"`sac agents logs {srv.agent_name}`, then retry that "
                    "notification; observe the same delivery operation at "
                    f"`/v1/exchanges/{exchange_id}`"
                )
                if native_cause is not None:
                    check = _admission_failure(exc, agent=srv.agent_name)
                elif native_visibility_required:
                    check = Check.unknown(
                        "hermes_transcript_visible",
                        "Hermes transcript visibility was not confirmed "
                        f"({type(exc).__name__}: {detail})",
                        hint,
                        cause=cause,
                    )
                else:
                    check = Check.unknown(
                        "tui_turn_admitted",
                        "the target TUI did not confirm turn admission "
                        f"({type(exc).__name__}: {detail})",
                        hint,
                        cause=cause,
                    )
                failure_check = check
                log.warning(
                    "turn exchange remains retryable exchange_id=%s check=%s",
                    exchange_id,
                    json.dumps(check.to_dict(), sort_keys=True),
                )
                status = (
                    StatusCode(
                        kind="http",
                        code=102,
                        message=(
                            f"{check.detail}; retryable non-final state; cause="
                            f"{cause.kind}/{cause.code}: {cause.message}; next: "
                            f"{check.hint}"
                        ),
                    )
                    if effective_visible_id
                    else cause
                )
            try:
                srv.exchange_finish(
                    exchange_id,
                    agent=srv.agent_name,
                    opened_at=opened_at,
                    status=status,
                )
            except Exception:  # stx-allow: fallback (reason: the accepted exchange remains non-final and findable when its completion cannot be written; never forge completion in process memory)
                log.exception("could not conclude turn exchange %s", exchange_id)
            finally:
                with srv.admission_lock:
                    if srv.active_delivery == (
                        delivery_key or exchange_id,
                        exchange_id,
                    ):
                        srv.active_delivery = None
            return status

        # A bare CCT wake has no exchange id it knows how to poll. Its current
        # client considers 202 final, which produced the observed false ACK:
        # Telegram showed delivered while the target harness had not proven
        # admission. Complete this compatibility-shaped request synchronously
        # and return 200 only after the adapter's proof; return non-2xx on a
        # retryable miss so CCT leaves the message unacknowledged. Durable
        # callers that supply either id retain the asynchronous 202 contract.
        if implicit_delivery:
            final_status = deliver()
            if final_status.code == 200:
                self._respond(
                    200,
                    {
                        "agent": srv.agent_name,
                        "exchange_id": exchange_id,
                        "delivery_id": effective_visible_id,
                        "receipt": {"state": "delivered", "final": True},
                        "status_code": final_status.to_dict(),
                    },
                )
            else:
                admission_check = failure_check or Check.unknown(
                    "hermes_transcript_visible",
                    final_status.message,
                    "Inspect the target TUI readiness and retry the same delivery.",
                )
                native_cause = admission_check.cause
                response_code = (
                    507
                    if native_cause is not None
                    and native_cause.code in {"ENOSPC", "EDQUOT"}
                    else 502
                )
                self._respond(
                    response_code,
                    {
                        "agent": srv.agent_name,
                        "exchange_id": exchange_id,
                        "delivery_id": effective_visible_id,
                        "error": final_status.message,
                        "receipt": {
                            "state": "retryable",
                            "final": False,
                            "error": final_status.message,
                        },
                        "check": admission_check.to_dict(),
                        "status_code": StatusCode(
                            kind="http",
                            code=response_code,
                            message=(
                                "the target TUI did not prove Telegram admission; "
                                f"exchange {exchange_id} remains retryable"
                            ),
                        ).to_dict(),
                    },
                )
            return

        threading.Thread(target=deliver, daemon=True).start()
        self._respond(
            202,
            {
                "exchange_id": exchange_id,
                "receipt": {
                    "state": "pending",
                    "final": False,
                    "delivery_mode": delivery_mode,
                },
                "status_code": StatusCode(
                    kind="http",
                    code=202,
                    message=(
                        f"turn delivery accepted for {srv.agent_name!r}; poll "
                        f"`/v1/exchanges/{exchange_id}` for the separately recorded result"
                    ),
                ).to_dict(),
            },
        )


# ---------------------------------------------------------------------------
# Lifecycle log
# ---------------------------------------------------------------------------
def write_bridge_event(
    stream: IO[str],
    event: str,
    *,
    agent: str,
    host: str,
    port: int,
    pid: int,
    now_fn: Callable[[], float] = time.time,
) -> str:
    """Write ONE lifecycle line to ``stream``, flush it, and return it.

    WHY THIS EXISTS: ``tui-turn-bridge.log`` was 0 bytes for 16 of the 17
    agents on the host. The launcher opens it (``open(..., "ab")`` in
    ``_tui_turn_bridge_lifecycle``) and hands it to the child as BOTH stdout
    and stderr — but the bridge never wrote a single line of its own, so the
    file only ever captured an UNHANDLED traceback. When 14 bridges were found
    dead on 2026-08-11, the cause of not one of those deaths could be
    recovered: no bind line to prove it ever served, no shutdown line to say
    whether it exited on a signal or vanished. A log that is empty on the happy
    path cannot bracket a failure.

    Two lines is the whole contract — one after the bind, one on the way out —
    so an operator reading the file can always answer "did it serve, and did it
    leave cleanly?". The flush is load-bearing: the child's stdio is a
    block-buffered pipe onto a file, so an unflushed bind line would be lost
    in exactly the crash it is meant to explain.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now_fn()))
    line = (
        f"{stamp} tui-turn-bridge {event} "
        f"agent={agent} host={host} port={port} pid={pid}\n"
    )
    stream.write(line)
    stream.flush()
    return line


def _emit(
    event: str, *, agent: str, host: str, port: int
) -> None:  # pragma: no cover - thin best-effort wrapper over write_bridge_event, which is unit-tested directly
    """Best-effort :func:`write_bridge_event` onto the launcher's log fd.

    Swallows deliberately: the log is a DIAGNOSTIC, and a bridge that cannot
    write its own log line must still serve turns — degrading wake-on-push to
    restore a log file would trade the incident for a worse one.
    """
    try:
        write_bridge_event(
            sys.stderr, event, agent=agent, host=host, port=port, pid=os.getpid()
        )
    except Exception as exc:  # stx-allow: fallback (reason: the lifecycle log is diagnostic only — an unwritable log fd must never stop the bridge from serving /v1/turn, which is the whole point of the process)
        log.warning("tui-turn-bridge: could not write %s log line: %s", event, exc)


def build_server(
    *,
    host: str,
    port: int,
    on_turn: Callable[..., None],
    agent_name: str,
    on_control: Callable[[str], None] | None = None,
    exchange_open: Callable[..., tuple[str, str]] = open_turn_exchange,
    exchange_finish: Callable[..., None] = finish_turn_exchange,
    exchange_read: Callable[[str], dict[str, Any] | None] = read_turn_exchange,
) -> _TurnBridgeServer:
    """Construct (but do not run) the bridge server. Test seam.

    A bind refusal (port still held by a lingering old bridge) is re-raised
    as a :class:`TurnBridgePortBusyError` naming the port + holder +
    remediation, not a bare ``OSError [Errno 98] Address already in use``.
    """
    try:
        return _TurnBridgeServer(
            (host, port),
            on_turn,
            agent_name,
            on_control,
            exchange_open=exchange_open,
            exchange_finish=exchange_finish,
            exchange_read=exchange_read,
        )
    except OSError as exc:
        raise port_busy_error(host, port, agent_name, cause=exc) from exc


def serve(  # pragma: no cover - integration entry: installs main-thread-only signal handlers + blocks in serve_forever; the server logic is unit-tested via build_server, the full serve path is exercised end-to-end
    *,
    host: str,
    port: int,
    on_turn: Callable[..., None],
    agent_name: str,
    on_control: Callable[[str], None] | None = None,
) -> None:
    """Run the bridge server until the process is signalled. Blocking."""
    # A listening /health socket must mean the bridge can persist the 202 it
    # promises. The 202 ledger is the acknowledgement boundary, so binding
    # while its PostgreSQL ACL is broken advertises a service that cannot
    # accept any turn. This read-only open performs no ownership repair.
    preflight_turn_exchange_store()
    server = build_server(
        host=host,
        port=port,
        on_turn=on_turn,
        agent_name=agent_name,
        on_control=on_control,
    )

    def _graceful(*_a: Any) -> None:
        # serve_forever() runs in the main thread here; shutdown() must be
        # called from another thread, so the signal handler spawns one.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    log.info("tui-turn-bridge: serving %s on %s:%d", agent_name, host, port)
    # The bind SUCCEEDED — record it in the durable per-agent log. Emitted here
    # rather than before ``build_server`` so the line is proof the socket is
    # actually held, not merely that the process started.
    _emit("bind", agent=agent_name, host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        # Brackets the bind line: its ABSENCE next to a bind line is itself the
        # diagnosis (the process was killed rather than signalled).
        _emit("shutdown", agent=agent_name, host=host, port=port)


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------
def _build_on_turn(
    config: AgentConfig, *, runtime: Any | None = None
) -> Callable[..., Any | None]:
    """Inject callback that drives one TUI turn through its runtime adapter.

    Calls :meth:`TuiSessionRuntime.send_turn` with ``wait_ready=False`` (see
    the inline note); raises when the session is gone so the handler answers
    502 (the subscriber's ``raise_for_status`` surfaces it loud). ``runtime``
    is a test seam. When the wake carries a ``from_agent``, the inbound is
    RECORDED into the DB-backed ledger BEFORE the inject so the ``Stop`` hook
    can push a dispatch-correlated report back (SDK-parity outbound — see
    :mod:`_tui_outbound`); best-effort — a ledger failure never blocks the
    turn (only the auto-report is lost).
    """
    if (
        runtime is None
    ):  # pragma: no cover - trivial default-construct of the real runtime
        from .._lifecycle._runtime_select import _get_runtime

        runtime = _get_runtime(config)

    def on_turn(
        text: str,
        *,
        from_agent: str | None = None,
        dispatch_id: str | None = None,
        visible_delivery_id: str | None = None,
        delivery_mode: str = "steer",
    ) -> object | None:
        if from_agent:
            try:
                from ._tui_outbound import record_dispatch

                # No state.db path any more: the inbound ledger is PostgreSQL.
                # `state_dir_for_config` is no longer imported here because it
                # was imported ONLY to build that path — and an import kept for
                # a vanished use is how a module keeps a dependency nobody can
                # see the reason for.
                record_dispatch(
                    agent=config.name,
                    from_agent=from_agent,
                    dispatch_id=dispatch_id,
                )
            except Exception as exc:  # stx-allow: fallback (reason: a ledger-write failure must not block delivering the wake — the agent still processes the turn; only the auto-completion-report is lost, logged at WARNING to stderr and the rotating ~/.scitex/logging/runtime/scitex-<date>.log via scitex-logging)
                slogging.getLogger(__name__).warning(
                    "tui-outbound: failed to record inbound dispatch for %s: %s",
                    config.name,
                    exc,
                )
        # ``wait_ready=False`` → skip the blocking modal DRAIN, which waits up
        # to 60s on a "? for shortcuts" marker an idle autonomous pane may
        # never render — fatal for a wake POST. Boot modals are already drained
        # by ``start()._drain_at_boot``, so a live wake needs no drain.
        #
        # THE ACCEPTANCE CHECK STILL RUNS. It is a separate flag precisely
        # because it must survive ``wait_ready=False``: this is the path
        # dispatch actually takes, so gating acceptance on the drain flag would
        # have left the real route unchecked. It costs one capture, not 60s.
        #
        # THE SENTENCE THAT USED TO END THIS COMMENT — "claude queues
        # keystrokes typed mid-turn, so a live wake ... is never dropped" — IS
        # FALSE and is why this bug survived. Measured 2026-08-18: four
        # dispatches to live agents across four pane states produced zero
        # completed tasks, one over a 35-minute window with no restart in it.
        # Claude does queue them; the queue does not reliably drain.
        visible_send = getattr(runtime, "send_visible_turn", None)
        if visible_delivery_id and callable(visible_send):
            marker = f"<!-- delivery:{visible_delivery_id} -->"
            visible_text = text if marker in text else f"{text}\n{marker}"
            delivered = visible_send(
                config,
                visible_text,
                visible_delivery_id=visible_delivery_id,
                delivery_mode=delivery_mode,
            )
        else:
            interactive_send = getattr(runtime, "send_interactive_turn", None)
            if callable(interactive_send):
                delivered = interactive_send(
                    config, text, delivery_mode=delivery_mode
                )
            elif delivery_mode == "queue":
                raise RuntimeError(
                    "this runtime does not expose explicit queue delivery"
                )
            else:
                delivered = runtime.send_turn(config, text, wait_ready=False)
        if not delivered:
            # Name the ACTUAL cause. This used to assert the session did not
            # exist, which was true when absence was the only cause and became
            # a misdiagnosis the moment a busy pane could also refuse — it sent
            # the reader hunting a dead agent that was in fact working.
            #
            # getattr-guarded because the REASON is enrichment and the RAISE is
            # the contract. A runtime seam that cannot explain itself must
            # still fail loudly; making the failure depend on the explainer
            # would let a missing method turn a refusal into a crash, or worse
            # into a silent success in some future caller that catches it.
            explain = getattr(runtime, "why_not_deliverable", None)
            why = (explain(config) if callable(explain) else None) or (
                "no reason available from this runtime — the session is absent, "
                "or the pane would park the turn rather than run it"
            )
            # NAME THE NEXT STEP, not only the cause. A refusal that explains
            # itself and stops leaves the sender with nowhere to go: scitex-hub
            # hit this on 2026-09-07, retried the same POST three times, got
            # three identical 502s and reported "cannot reach sac" — correct
            # behaviour against a contract that never told it what else to do.
            #
            # The two causes want OPPOSITE responses and the sender cannot act
            # without being told which it is:
            #   BUSY   -> the agent is working; the turn was NOT stored, so
            #             resend later or use a durable rail
            #   ABSENT -> nothing will drain; retrying forever is pointless and
            #             someone must start it
            #
            # Explicit about the loss, because that is the part a sender gets
            # wrong: a 502 here means NOTHING WAS QUEUED. Saying so is what
            # stops a peer assuming the message is waiting somewhere.
            raise RuntimeError(
                f"turn NOT delivered to agent {config.name!r}: {why}\n"
                "NOTHING WAS QUEUED — this turn is not stored anywhere and "
                "will not be retried by sac.\n"
                "NEXT: if the agent is BUSY, resend when it idles, or use a "
                "durable rail (scitex-cards DM) that survives a busy pane. "
                "If the agent is ABSENT, resending cannot help — start it "
                f"with `sac agents start {config.name}` and check "
                f"`sac agents list {config.name}` first."
            )
        # Preserve the historical callback contract for bool-returning TUI
        # adapters while carrying Hermes' structured native receipt through
        # to the exchange worker.
        return None if isinstance(delivered, bool) else delivered

    # Read by the bridge handler to require responder-issued visibility IDs
    # for ordinary SAC turns.  A named capability is preferable to guessing
    # from a callback's return value after it may already have lost the input.
    on_turn._sac_requires_visible_delivery = callable(  # type: ignore[attr-defined]
        getattr(runtime, "send_visible_turn", None)
    )
    return on_turn


def _build_on_control(
    config: AgentConfig, *, runtime: Any | None = None
) -> Callable[[str], None]:
    if runtime is None:
        from .._lifecycle._runtime_select import _get_runtime

        runtime = _get_runtime(config)

    def on_control(key: str) -> None:
        send_key = getattr(runtime, "send_key", None)
        if not callable(send_key):
            raise RuntimeError("runtime does not expose explicit UI controls")
        if not send_key(config, key):
            raise RuntimeError("the live TUI tmux session is absent")

    return on_control


def main(
    argv: list[str] | None = None,
) -> int:  # pragma: no cover - subprocess entry: parses args, loads the spec, and blocks in serve(); exercised end-to-end (the launcher spawns it), not unit
    parser = argparse.ArgumentParser(prog="tui-turn-bridge")
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--port", type=int, required=True)
    # No literal default: an omitted --host resolves from the spec the bridge
    # is about to serve (``resolved_a2a_host``), which itself falls back to
    # DEFAULT_HOST. The launcher always passes --host explicitly; this keeps a
    # hand-run bridge on the SAME address as its spec instead of loopback.
    parser.add_argument("--host", default=None)
    args = parser.parse_args(argv)

    from ..config import load_config

    config = load_config(args.config_path)
    serve(
        host=args.host or resolved_a2a_host(config),
        port=args.port,
        on_turn=_build_on_turn(config),
        on_control=_build_on_control(config),
        agent_name=config.name,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())


__all__ = [
    "resolved_a2a_host",
    "resolved_a2a_port",
    "is_turn_route",
    "is_control_route",
    "build_server",
    "serve",
    "main",
    "start_turn_bridge",
    "stop_turn_bridge",
    "TurnBridgePortBusyError",
    "port_busy_error",
    "port_is_free",
    "PID_FILENAME",
    "LOG_FILENAME",
    "MODULE_PATH",
    "DEFAULT_HOST",
    "_pid_path",
    "_state_dir",
]
