"""Host-side A2A ``/v1/turn`` → tmux bridge for ``runtime: tui`` agents.

Closes the wake-on-push gap for interactive TUI agents. The SDK runtime
serves ``/v1/turn`` from its in-SIF runner so the ``sac mcp channel``
subscriber's wake POST (``_mcp/_channel_wake._wake_turn``) DRIVES an idle
agent to act; the TUI runtime runs ``claude`` in tmux with no in-process
HTTP server, so that POST hit a dead port and the message never woke it.
This module gives TUI agents the SAME endpoint host-side (the in-SIF
subscriber POSTs to ``127.0.0.1:<port>`` — apptainer shares the host net
namespace): on ``POST /v1/turn`` it injects ``text`` into the tmux session
via :meth:`TuiSessionRuntime.send_turn` and returns ``200`` once delivered.

Wire format mirrors ``_session_http`` so ``_wake_turn`` + A2A clients work
unchanged:

    POST /v1/turn                      (bare — the port identifies the agent)
    POST /agents/<name>/turn           (canonical sac namespace)
    POST /agents/<name>/send           (A2A v1 alias)
    Content-Type: application/json
    {"text": "...", "from_agent": "<peer>"?, "dispatch_id": "<id>"?}

    200 {"text": "", "delivered": true, "mode": "tui-tmux-inject", "agent": "<name>"}
    400 {"error": "missing or empty 'text' field"}        # schema mismatch, loud
    404 {"error": "..."}                                  # unknown route / wrong agent
    502 {"error": "tui inject failed: ..."}               # session gone / input wedged

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
import json
import logging
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any, Callable

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

log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__(server_address, _TurnBridgeHandler)
        self.on_turn = on_turn
        self.agent_name = agent_name


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
        if self.path.split("?", 1)[0].rstrip("/") == "/health":
            self._respond(200, {"status": "ok", "agent": self._srv().agent_name})
            return
        self._respond(404, {"error": f"no GET route {self.path!r}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler contract)
        srv = self._srv()
        # Drain the request body FIRST (even on a route miss) so a 404
        # never leaves an unread body on the socket.
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not is_turn_route(self.path, srv.agent_name):
            self._respond(404, {"error": f"no turn route {self.path!r}"})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            self._respond(400, {"error": f"bad JSON: {exc}"})
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
        from_agent = raw_from if isinstance(raw_from, str) and raw_from else None
        dispatch_id = raw_did if isinstance(raw_did, str) and raw_did else None
        try:
            srv.on_turn(text, from_agent=from_agent, dispatch_id=dispatch_id)
        except Exception as exc:  # stx-allow: fallback (reason: surface inject failure as 502 instead of crashing the bridge; the wake POST's raise_for_status then propagates it loud to the channel subscriber)
            self._respond(502, {"error": f"tui inject failed: {exc}"})
            return
        self._respond(
            200,
            {
                "text": "",
                "delivered": True,
                "mode": "tui-tmux-inject",
                "agent": srv.agent_name,
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
    *, host: str, port: int, on_turn: Callable[..., None], agent_name: str
) -> _TurnBridgeServer:
    """Construct (but do not run) the bridge server. Test seam.

    A bind refusal (port still held by a lingering old bridge) is re-raised
    as a :class:`TurnBridgePortBusyError` naming the port + holder +
    remediation, not a bare ``OSError [Errno 98] Address already in use``.
    """
    try:
        return _TurnBridgeServer((host, port), on_turn, agent_name)
    except OSError as exc:
        raise port_busy_error(host, port, agent_name, cause=exc) from exc


def serve(  # pragma: no cover - integration entry: installs main-thread-only signal handlers + blocks in serve_forever; the server logic is unit-tested via build_server, the full serve path is exercised end-to-end
    *, host: str, port: int, on_turn: Callable[..., None], agent_name: str
) -> None:
    """Run the bridge server until the process is signalled. Blocking."""
    server = build_server(host=host, port=port, on_turn=on_turn, agent_name=agent_name)

    def _graceful(*_a: Any) -> None:
        # serve_forever() runs in the main thread here; shutdown() must be
        # called from another thread, so the signal handler spawns one.
        import threading

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
) -> Callable[..., None]:
    """Inject callback that drives one TUI turn via the tmux PTY.

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
        from .tui_session import TuiSessionRuntime

        runtime = TuiSessionRuntime()

    def on_turn(
        text: str,
        *,
        from_agent: str | None = None,
        dispatch_id: str | None = None,
    ) -> None:
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
                logging.getLogger(__name__).warning(
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
            raise RuntimeError(
                f"turn NOT delivered to agent {config.name!r}: {why}"
            )

    return on_turn


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
        agent_name=config.name,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())


__all__ = [
    "resolved_a2a_host",
    "resolved_a2a_port",
    "is_turn_route",
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
