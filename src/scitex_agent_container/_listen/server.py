"""Starlette app factory for ``sac listen``.

Hosts the canonical ``/agents/...`` control-plane namespace (ADR-0004).
The legacy ``/v1/sac/`` paths and ``/v1/a2a/`` protocol-compat mirror
were dropped wholesale per D13 (no backward compat). v1 endpoints:

    GET    /v1/health
    GET    /.well-known/agent-card.json  (A2A v1 fleet AgentCard)
    GET    /agents                       (list)
    POST   /agents                       (create/start from spec)
    GET    /agents/<name>/status
    GET    /agents/<name>/tail           (SSE stream of session.jsonl)
    POST   /agents/<name>/send           (prompt or key)
    GET    /agents/<name>/.well-known/agent-card.json
                                         (A2A v1 per-agent AgentCard)
    DELETE /agents/<name>

The agent-card paths follow A2A v1.0's canonical well-known location
(``/.well-known/agent-card.json``) — same as the ``a2a/_server.py``
surface. The pre-v1 ``/agents/<name>/card`` route was dropped per
ADR-0004 (no backward compat).
"""

from __future__ import annotations

import asyncio
import json
import json as _json
import os
import shutil
import subprocess
import urllib.error as _urlerror
import urllib.request as _urlrequest
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .._runners._session_state import read_session_id, state_dir_for
from .._state.registry import Registry
from ..a2a._inbox_bus import mint_event
from ..config import load_config
from ..config._resolve import resolve_config
from ._acl import NodeAuthMiddleware, check_send_acl, deny_response
from ._inline_spec import materialize_inline_spec
from ._nodes import Broker, NodeRegistry
from .auth import BearerAuthMiddleware

# Re-exported under the module's public surface so unit tests can patch
# them as ``scitex_agent_container._listen.server._urlrequest.urlopen``
# / ``._urlerror.URLError`` without forcing every call site to alias.
__all__ = ["create_app", "_urlrequest", "_urlerror"]


def _find_claude_binary() -> str:
    """Same resolver as send_cmds — bundled SDK copy first, then PATH."""
    bundled = (
        "/opt/venv-sac/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
    )
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude binary not found")
    return found


# --- Handlers --------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "sac-listen", "v": 1})


async def list_agents(_request: Request) -> JSONResponse:
    """List agents the local Registry knows about."""
    try:
        reg = Registry()
        rows = reg.list_all()
    except Exception as exc:  # stx-allow: fallback (reason: surface a JSON error to the caller rather than ASGI 500 stack)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"agents": rows})


async def agent_status(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    return JSONResponse(
        {
            "name": name,
            "spec_path": str(spec_path),
            "workdir": cfg.expanded_workdir,
            "session_id": sid,
            "state_dir": str(sd),
        }
    )


from ._forward import forward_to_live_runner  # noqa: E402


async def agent_send(request: Request) -> Response:
    """POST /agents/<name>/send.

    Body discriminator (per REQUIREMENT_SUMMARY §4.2):
        {"type":"prompt","prompt":"...","options":{...}}
        {"type":"key","key":"ESC"}

    Back-compat (this commit only): a body without ``type`` is treated
    as ``{type: "prompt", ...}`` so existing callers keep working.

    Routing for ``type: prompt``:
        1. If the agent has ``spec.a2a.port`` set and its inbound HTTP
           is reachable, forward the turn into the live in-memory
           runner inbox.
        2. Otherwise fall back to ``claude --resume <sid> -p`` —
           short-lived re-launch against the persisted session.jsonl.

    Routing for ``type: key``:
        SIGINT the live runner pid (best-effort). ESC / C-c / SIGINT
        accepted; unknown keys → 400. No live runner → 404.
    """
    name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (reason: malformed JSON → 400 with explanation rather than ASGI 500)
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    # Default to prompt when ``type`` is absent — back-compat shim
    # documented in REQUIREMENT_SUMMARY §4.2.
    type_ = body.get("type", "prompt")
    if type_ == "key":
        key = body.get("key")
        # Supported: ESC / C-c / SIGINT — all map to SIGINT on the
        # runner pid, which interrupts the current turn without
        # killing the agent.
        if key not in ("ESC", "C-c", "SIGINT"):
            return JSONResponse(
                {
                    "error": (
                        f"unsupported key={key!r}; expected one of "
                        "'ESC', 'C-c', 'SIGINT'"
                    )
                },
                status_code=400,
            )
        import signal as _signal

        sd = state_dir_for(name)
        pid_file = sd / "pid"
        if not pid_file.is_file():
            return JSONResponse(
                {"error": f"agent {name!r} has no live session"},
                status_code=404,
            )
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, _signal.SIGINT)
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(
            {
                "name": name,
                "route": "interrupt",
                "pid": pid,
                "signal": "SIGINT",
            }
        )
    if type_ != "prompt":
        return JSONResponse(
            {"error": f"unknown type {type_!r}; expected 'prompt' or 'key'"},
            status_code=400,
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return JSONResponse(
            {"error": "missing or empty 'prompt' string"}, status_code=400
        )

    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    # 1) Try live-runner route first.
    options = body.get("options") or {}
    live = await forward_to_live_runner(cfg, name, prompt, options)
    if live is not None:
        return live

    # 2) Fall back to short-lived re-launch.
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    if not sid:
        return JSONResponse(
            {"error": f"no session_id recorded for {name!r}"}, status_code=409
        )

    try:
        claude_bin = _find_claude_binary()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    argv = [claude_bin, "--resume", sid, "-p", prompt]
    if "model" in options:
        argv += ["--model", str(options["model"])]
    if "max_turns" in options:
        argv += ["--max-turns", str(options["max_turns"])]

    workdir = cfg.expanded_workdir or os.getcwd()

    # SSE branch: client opted in via Accept: text/event-stream. Stream
    # claude's stdout line-by-line as SSE frames.
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        argv += ["--output-format", "stream-json", "--include-partial-messages"]
        return StreamingResponse(
            _stream_claude(argv, workdir, name, sid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Buffered branch (default): run to completion, return one JSON blob.
    proc = await asyncio.to_thread(
        subprocess.run,
        argv,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return JSONResponse(
        {
            "name": name,
            "session_id": sid,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    )


def _sse_frame(event: str | None, data: str) -> bytes:
    """Encode one SSE frame. ``event`` is optional; ``data`` is one line."""
    head = f"event: {event}\n" if event else ""
    return (head + f"data: {data}\n\n").encode("utf-8")


async def _stream_claude(argv: list[str], workdir: str, name: str, sid: str):
    """Run claude as an async subprocess and yield SSE frames."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        yield _sse_frame("error", _json.dumps({"error": str(exc)}))
        return

    yield _sse_frame("start", _json.dumps({"name": name, "session_id": sid}))

    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield _sse_frame(None, line.decode("utf-8", "replace").rstrip("\n"))
        rc = await proc.wait()
        yield _sse_frame("done", _json.dumps({"returncode": rc}))
    except (asyncio.CancelledError, GeneratorExit):
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
        raise


# --- tail (SSE over session.jsonl) — extracted to ``_tail.py`` -------------

from ._tail import agent_tail  # noqa: F401, E402  (re-exported for routes)


async def agents_start(request: Request) -> JSONResponse:
    """POST /agents — start one or more agents.

    Body shapes:

        # Start a pre-registered spec (existing on disk):
        {"name": "<existing-spec-name>"}

        # Register-and-start an ad-hoc spec in one call:
        {
            "name": "<name>",
            "spec": {"apiVersion": "scitex-agent-container/v3",
                     "kind": "Agent",
                     "spec": {...}},
            "overwrite": false   # optional; default false → 409 on clash
        }
    """
    try:
        body = await request.json()
    except (
        Exception
    ):  # stx-allow: fallback (reason: malformed JSON → 400 instead of 500)
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return JSONResponse(
            {"error": "missing or empty 'name' string"}, status_code=400
        )

    inline_spec = body.get("spec")
    if inline_spec is not None:
        err = materialize_inline_spec(
            name, inline_spec, overwrite=bool(body.get("overwrite"))
        )
        if err is not None:
            return err

    sac_bin = shutil.which("sac") or "sac"
    proc = await asyncio.to_thread(
        subprocess.run,
        [sac_bin, "agent", "start", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return JSONResponse(
        {
            "name": name,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        status_code=200 if proc.returncode == 0 else 502,
    )


async def agent_card(request: Request) -> JSONResponse:
    """GET /agents/<name>/.well-known/agent-card.json.

    Resolution order (handoff §4 — A2A compliance for both kinds of
    node):
      1. sac-managed agent — look up the YAML via ``resolve_config``
         and project the v3 spec onto a v1 AgentCard.
      2. external node — return the synthesised card cached by
         :class:`NodeRegistry` (registered implicitly on first
         ``message:send`` / ``inbox/stream`` touch).

    Only 404 when *neither* path can produce a card.
    """
    import yaml

    from ..a2a._card import project_card

    name = request.path_params["name"]
    base_url = str(request.base_url).rstrip("/")

    # 1) sac-managed (YAML-backed) — preserve the existing behaviour.
    try:
        spec_path = resolve_config(name)
    except Exception:
        spec_path = None
    if spec_path is not None:
        try:
            with open(spec_path, encoding="utf-8") as fh:
                v3 = yaml.safe_load(fh) or {}
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(project_card(name, v3, base_url))

    # 2) external node — synthesised card cached at registration.
    nodes: NodeRegistry = request.app.state.nodes
    card = nodes.card(name)
    if card is not None:
        return JSONResponse(card)

    return JSONResponse(
        {"error": f"unknown agent or node: {name!r}"}, status_code=404
    )


async def fleet_card_handler(request: Request) -> JSONResponse:
    """GET /.well-known/agent-card.json.

    A2A v1.0 canonical fleet AgentCard. Lists every agent currently
    known to the local ``Registry`` under the
    ``x-scitex-agent-container.agents[]`` extension namespace; per-agent
    cards live at ``/agents/<name>/.well-known/agent-card.json``.
    """
    from ..a2a._card import fleet_card

    try:
        reg = Registry()
        rows = reg.list_all()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    agents = sorted(r["name"] for r in rows if isinstance(r, dict) and "name" in r)
    base_url = str(request.base_url).rstrip("/")
    card = fleet_card(base_url, agents)
    return JSONResponse(card)


# --- WI-3 external nodes: inbox endpoints on the host control plane ------
#
# The handoff (HANDOFF_AGENT_COMMS_2026-05-19.md §4) puts the inbox
# endpoints (``message:send`` and ``inbox/stream``) on the always-on
# ``sac listen`` host control-plane and makes them keyed by **node
# identity** — they must accept a name that has no YAML and no
# container. The handlers below are the implementation of that
# requirement.
#
# Routes registered in ``_v1_agent_routes`` below:
#
#   POST /agents/{name}/message:send  → node_message_send
#   GET  /agents/{name}/inbox/stream  → node_inbox_stream
#
# The agent-card route is *not* overridden here — instead
# :func:`agent_card` falls back to the synthesised card for nodes
# that are not YAML-backed. That fall-back is added to the existing
# handler below.


async def node_message_send(request: Request) -> Response:
    """``POST /agents/<name>/message:send`` — publish an A2A
    ``SendMessage`` body to the local node's inbox bus.

    Implicitly registers ``<name>`` as an external node on first use
    so the synthesised AgentCard is available for the well-known
    lookup. The publish is **always loud** — a malformed body returns
    400, never a silent drop (handoff §0 Hard rules).

    WI-2 ACL gate: every send is checked by
    :func:`_acl.check_send_acl` before publish:

    * A per-node bearer pins identity — ``metadata.from_agent`` must
      match the bearer's resolved name; mismatch → 403.
    * Cross-group is denied by default; intra-group (parent↔child
      and sibling↔sibling) is allowed.
    * The host-wide bearer is treated as an administrative caller
      whose ``metadata.from_agent`` is honoured verbatim — the
      cross-host-forwarding seam (handoff §4 WI-4 prerequisite).

    Bearer auth (outer perimeter) is still enforced by
    :class:`BearerAuthMiddleware`; identity resolution is enforced
    by :class:`NodeAuthMiddleware`.
    """
    name = request.path_params["name"]
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(
            {"error": f"body must be valid JSON: {exc}"}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "body must be a JSON object"}, status_code=400
        )

    method = body.get("method")
    if method not in ("message/send", "SendMessage", "SendStreamingMessage"):
        return JSONResponse(
            {
                "error": (
                    f"unsupported method {method!r}; expected one of "
                    "'message/send', 'SendMessage', 'SendStreamingMessage'"
                )
            },
            status_code=400,
        )

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse(
            {"error": "params must be a JSON object"}, status_code=400
        )
    message = params.get("message") or {}
    parts = message.get("parts") if isinstance(message, dict) else None
    text = ""
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                text += p["text"]

    # sac-extension metadata: same convention as a2a/_server.py — under
    # ``params.metadata`` first, then ``message.metadata`` as a
    # secondary, since some clients prefer message-scoped metadata.
    sac_meta: dict[str, Any] = {}
    for src in (params.get("metadata"), message.get("metadata")):
        if isinstance(src, dict):
            sac_meta.update(src)

    # WI-2 ACL check. ``authenticated_node`` is set by
    # :class:`NodeAuthMiddleware` — ``None`` means the host-wide
    # bearer was presented (administrative caller). The check honours
    # ``metadata.from_agent`` only when no per-node bearer is bound
    # (or when it matches), so a metadata field cannot spoof identity.
    authenticated_node = getattr(
        request.state, "authenticated_node", None
    )
    decision, reason = check_send_acl(
        authenticated_node=authenticated_node,
        claimed_from_agent=sac_meta.get("from_agent"),
        target=name,
    )
    if decision == "deny":
        return deny_response(reason or "ACL deny")

    event = mint_event(
        name,
        content=text,
        from_agent=sac_meta.get("from_agent"),
        conversation_id=sac_meta.get("conversation_id"),
        in_reply_to=sac_meta.get("in_reply_to"),
        priority=str(sac_meta.get("priority", "normal")),
        requires_reply=bool(sac_meta.get("requires_reply", False)),
    )

    # Implicit registration — handoff §4 "A2A compliance without a
    # YAML": synthesise the card the first time the name is touched.
    base_url = str(request.base_url).rstrip("/")
    nodes: NodeRegistry = request.app.state.nodes
    broker: Broker = request.app.state.inbox
    nodes.register(name, base_url)

    delivered = await broker.publish(name, event)
    return JSONResponse(
        {
            "msg_id": event["msg_id"],
            "to_agent": name,
            "delivered_subscriber_count": delivered,
        }
    )


async def node_inbox_stream(request: Request) -> Response:
    """``GET /agents/<name>/inbox/stream`` — SSE: one frame per event
    published to ``<name>`` on this sac listen.

    Consumed by ``sac mcp channel --name <name>`` inside an external
    node's Claude session (or a sac-managed agent's container). The
    frame shape is identical to ``a2a/_server.py``'s stream so the
    same client adapter works for both kinds of node.

    Implicitly registers ``<name>`` as an external node on first
    connect.
    """
    from starlette.responses import StreamingResponse

    name = request.path_params["name"]
    base_url = str(request.base_url).rstrip("/")
    nodes: NodeRegistry = request.app.state.nodes
    broker: Broker = request.app.state.inbox
    nodes.register(name, base_url)

    queue = await broker.subscribe(name)

    async def stream():
        try:
            # Comment-only frame so HTTP clients see the connection
            # open immediately (and tests can race-free detect "I'm
            # subscribed" before publishing).
            yield b": sac-channel ready\n\n"
            while True:
                if await request.is_disconnected():
                    return
                event = await queue.get()
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: message\ndata: {data}\n\n".encode("utf-8")
        finally:
            await broker.unsubscribe(name, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def agent_delete(request: Request) -> JSONResponse:
    """DELETE /agents/<name> — stop the agent."""
    name = request.path_params["name"]
    sd = state_dir_for(name)
    pid_file = sd / "pid"
    if not pid_file.is_file():
        return JSONResponse({"error": "no pid file"}, status_code=404)
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"name": name, "stopped": True, "pid": pid})


# --- App factory -----------------------------------------------------------


def _v1_agent_routes(prefix: str) -> list[Route]:
    """Build the agent route set under ``prefix`` (ADR-0004: only ``/agents``).

    Includes the WI-3 inbox endpoints (``message:send`` and
    ``inbox/stream``) which are keyed by node identity — they serve
    sac-managed agents and external nodes equally.
    """
    return [
        Route(f"{prefix}", list_agents, methods=["GET"]),
        Route(f"{prefix}", agents_start, methods=["POST"]),
        Route(f"{prefix}/{{name}}/status", agent_status, methods=["GET"]),
        Route(f"{prefix}/{{name}}/tail", agent_tail, methods=["GET"]),
        Route(f"{prefix}/{{name}}/send", agent_send, methods=["POST"]),
        # WI-3 — node-identity-keyed inbox endpoints.
        Route(
            f"{prefix}/{{name}}/message:send",
            node_message_send,
            methods=["POST"],
        ),
        Route(
            f"{prefix}/{{name}}/inbox/stream",
            node_inbox_stream,
            methods=["GET"],
        ),
        Route(
            f"{prefix}/{{name}}/.well-known/agent-card.json",
            agent_card,
            methods=["GET"],
        ),
        Route(f"{prefix}/{{name}}", agent_delete, methods=["DELETE"]),
    ]


def create_app(*, token: str) -> Starlette:
    """Build the Starlette app with bearer auth (ADR-0004 — ``/agents`` only).

    WI-3 wires a per-app :class:`Broker` + :class:`NodeRegistry` so
    external nodes (no YAML, no container) can attach as first-class
    members of the comms graph. The state lives on ``app.state`` so
    every handler shares the same broker and registry instance.

    WI-2 chains :class:`NodeAuthMiddleware` after
    :class:`BearerAuthMiddleware`: the outer middleware admits any
    request bearing a valid token (host-wide or per-node); the inner
    one resolves that token to a node identity and attaches it to
    ``request.state.authenticated_node`` so the ACL gate in
    :func:`node_message_send` can decide allow / deny.

    Middleware order matters. Starlette executes the *outermost*
    ``add_middleware`` call first (it wraps the app last but runs
    first on the inbound path). So the BearerAuthMiddleware call
    below comes **last** to make it the outermost layer.
    """
    routes: list[Route] = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/.well-known/agent-card.json", fleet_card_handler, methods=["GET"]),
    ]
    routes += _v1_agent_routes("/agents")
    app = Starlette(routes=routes)
    # Per-app shared state for the WI-3 inbox surface.
    app.state.inbox = Broker()
    app.state.nodes = NodeRegistry()
    # WI-2 — identity resolution (inner). Reads the same Bearer the
    # outer middleware already validated; tags ``request.state`` with
    # the resolved node name (or ``None`` for the host-wide bearer).
    app.add_middleware(NodeAuthMiddleware, host_bearer=token)
    # Outer perimeter — admits any valid token, rejects everything else.
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
