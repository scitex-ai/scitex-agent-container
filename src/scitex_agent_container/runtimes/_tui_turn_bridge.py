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
bridge must never block agent start/stop. Bound to ``127.0.0.1`` (loopback
wake POST; the bind is the security boundary, matching the SDK runner's
unauthed endpoint).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..config import AgentConfig
from ._tui_turn_bridge_lifecycle import (
    DEFAULT_HOST,
    LOG_FILENAME,
    MODULE_PATH,
    PID_FILENAME,
    _pid_path,
    _state_dir,
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
def is_turn_route(path: str, agent_name: str) -> bool:
    """True iff ``path`` is a turn-delivery route for ``agent_name``.

    Accepts the bare ``/v1/turn`` (the port already identifies the agent)
    and the named ``/agents/<agent_name>/{turn,send}`` aliases. A named
    route for a DIFFERENT agent is rejected (the caller returns 404) so a
    misrouted POST fails loud rather than landing in the wrong session.
    """
    clean = path.split("?", 1)[0].rstrip("/")
    if clean == "/v1/turn":
        return True
    if agent_name and clean in (
        f"/agents/{agent_name}/turn",
        f"/agents/{agent_name}/send",
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
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            self._respond(400, {"error": "missing or empty 'text' field"})
            return
        # Requester identity (optional) — the peer that dispatched this
        # wake. Threaded to on_turn so the inbound is recorded in the
        # ledger for the Stop-hook completion report (SDK parity). Absent
        # for an operator send / boot turn → no report is owed.
        raw_from = body.get("from_agent") if isinstance(body, dict) else None
        raw_did = body.get("dispatch_id") if isinstance(body, dict) else None
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
    try:
        server.serve_forever()
    finally:
        server.server_close()


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
                from .tui_session import state_dir_for_config

                record_dispatch(
                    db_path=state_dir_for_config(config) / "state.db",
                    agent=config.name,
                    from_agent=from_agent,
                    dispatch_id=dispatch_id,
                )
            except Exception as exc:  # stx-allow: fallback (reason: a ledger-write failure must not block delivering the wake — the agent still processes the turn; only the auto-completion-report is lost, logged for the operator)
                logging.getLogger(__name__).warning(
                    "tui-outbound: failed to record inbound dispatch for %s: %s",
                    config.name,
                    exc,
                )
        # ``wait_ready=False`` → the bare send_text_and_submit primitive
        # (text + Enter). The full ``wait_until_input_ready`` drain blocks up
        # to 60s on a "? for shortcuts" marker an idle autonomous pane may
        # never render — fatal for a wake POST; boot modals are already
        # drained by ``start()._drain_at_boot`` and claude queues keystrokes
        # typed mid-turn, so a live wake needs no drain and is never dropped.
        delivered = runtime.send_turn(config, text, wait_ready=False)
        if not delivered:
            raise RuntimeError(
                f"TUI session for agent {config.name!r} does not exist; "
                "cannot deliver the pushed turn."
            )

    return on_turn


def main(
    argv: list[str] | None = None,
) -> int:  # pragma: no cover - subprocess entry: parses args, loads the spec, and blocks in serve(); exercised end-to-end (the launcher spawns it), not unit
    parser = argparse.ArgumentParser(prog="tui-turn-bridge")
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args(argv)

    from ..config import load_config

    config = load_config(args.config_path)
    serve(
        host=args.host,
        port=args.port,
        on_turn=_build_on_turn(config),
        agent_name=config.name,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())


__all__ = [
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
