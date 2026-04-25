"""Stdlib HTTP server for the A2A protocol surface (sac-side).

Routes (mirroring the spec):

* ``GET /.well-known/agent.json`` — fleet AgentCard
* ``GET /v1/agents/`` — JSON list of agents
* ``GET /v1/agents/<name>/.well-known/agent.json`` — per-agent AgentCard
* ``POST /v1/agents/<name>`` — JSON-RPC ``tasks/send`` (other methods → -32601)

Backed by :func:`scitex_agent_container.a2a._card.project_card` for
projection and a single configurable handler for dispatch (see
:mod:`._handlers`).

Designed for orochi-free standalone use: no fleet imports, no auth,
no tunnel. A lab can run

    sac a2a serve mock-echo.yaml --port 8888

and curl ``http://localhost:8888/.well-known/agent.json``.
"""

from __future__ import annotations

import json
import logging
import socket
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import yaml

from scitex_agent_container.a2a._card import fleet_card, project_card
from scitex_agent_container.a2a._handlers import HANDLERS, HandlerError

log = logging.getLogger(__name__)

HandlerFn = Callable[[str, str], str]

_TASKS: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _A2AContext:
    """Per-server config: agent registry + dispatch handler."""

    def __init__(
        self,
        yamls: dict[str, dict[str, Any]],
        handler: HandlerFn,
    ) -> None:
        self.yamls = yamls
        self.handler = handler


class _A2AHandler(BaseHTTPRequestHandler):
    server_version = "scitex-agent-container-a2a/1"
    a2a: _A2AContext  # set on the server class

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _base_url(self) -> str:
        scheme = "http"
        host = self.headers.get("Host") or "localhost"
        return f"{scheme}://{host}"

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- GET ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        base = self._base_url()
        agents = sorted(self.a2a.yamls.keys())

        if path == "/.well-known/agent.json":
            self._send_json(200, fleet_card(base, agents))
            return
        if path == "/v1/agents/":
            self._send_json(
                200,
                {
                    "agents": [
                        {"name": n, "url": f"{base}/v1/agents/{n}"} for n in agents
                    ]
                },
            )
            return
        if path.startswith("/v1/agents/") and path.endswith("/.well-known/agent.json"):
            name = path[len("/v1/agents/") : -len("/.well-known/agent.json")]
            v3 = self.a2a.yamls.get(name)
            if v3 is None:
                self._send_json(404, {"error": f"unknown agent: {name}"})
                return
            self._send_json(200, project_card(name, v3, base))
            return

        self._send_json(404, {"error": f"not found: {path}"})

    # --- POST --------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/v1/agents/"):
            self._send_json(404, {"error": f"not found: {path}"})
            return
        name = path[len("/v1/agents/") :].rstrip("/")
        v3 = self.a2a.yamls.get(name)
        if v3 is None:
            self._send_json(404, {"error": f"unknown agent: {name}"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode() or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"bad JSON: {exc}"})
            return

        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "tasks/get":
            tid = params.get("id")
            if not tid or tid not in _TASKS:
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32000, "message": f"task not found: {tid}"},
                    },
                )
                return
            self._send_json(
                200, {"jsonrpc": "2.0", "id": rpc_id, "result": _TASKS[tid]}
            )
            return

        if method != "tasks/send":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32601,
                        "message": f"method not found: {method}",
                    },
                },
            )
            return

        msg = params.get("message", {}) or {}
        parts = msg.get("parts", []) or []
        user_text = next(
            (p.get("text", "") for p in parts if p.get("type") == "text"),
            "",
        )

        try:
            reply = self.a2a.handler(name, user_text)
            state = "completed"
            err_msg = None
        except HandlerError as exc:
            reply = str(exc)
            state = "failed"
            err_msg = {"text": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("handler crashed for %s", name)
            reply = f"handler crashed: {exc}"
            state = "failed"
            err_msg = {"text": str(exc)}

        task = {
            "id": params.get("id") or f"task-{uuid.uuid4().hex[:12]}",
            "sessionId": params.get("sessionId"),
            "status": {"state": state, "message": err_msg, "timestamp": _now_iso()},
            "history": [
                msg,
                {"role": "agent", "parts": [{"type": "text", "text": reply}]},
            ],
            "artifacts": [],
            "metadata": {
                "x-scitex-agent-container": {
                    "agent": name,
                    "served_by": "sac-a2a",
                    "generated_at": _now_iso(),
                }
            },
        }
        _TASKS[task["id"]] = task
        self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": task})


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def _agent_name_from_yaml(path: Path, v3: dict[str, Any]) -> str:
    """Use ``metadata.name`` if present, else the YAML filename stem."""
    meta = v3.get("metadata") or {}
    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return path.stem


def serve(
    agent_yamls: list[Path],
    *,
    host: str = "127.0.0.1",
    port: int = 8888,
    handler: HandlerFn | str = "echo",
) -> None:
    """Run the A2A HTTP server in the foreground."""
    if isinstance(handler, str):
        if handler not in HANDLERS:
            raise ValueError(
                f"unknown handler {handler!r}; pick one of {sorted(HANDLERS)}"
            )
        handler_fn = HANDLERS[handler]
    else:
        handler_fn = handler

    yamls: dict[str, dict[str, Any]] = {}
    for p in agent_yamls:
        v3 = _load_yaml(p)
        yamls[_agent_name_from_yaml(p, v3)] = v3

    if not yamls:
        raise ValueError("no agent YAMLs supplied")

    ctx = _A2AContext(yamls=yamls, handler=handler_fn)

    cls = type(
        "BoundA2AHandler",
        (_A2AHandler,),
        {"a2a": ctx},
    )

    httpd = ThreadingHTTPServer((host, port), cls)
    actual = httpd.server_address[:2]
    log.info(
        "sac-a2a listening on http://%s:%d (agents: %s, handler: %s)",
        actual[0],
        actual[1],
        ", ".join(sorted(yamls)),
        getattr(handler_fn, "__name__", "?"),
    )
    try:
        socket.setdefaulttimeout(60)
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("sac-a2a stopping (KeyboardInterrupt)")
    finally:
        httpd.server_close()
