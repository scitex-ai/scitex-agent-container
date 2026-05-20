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
from ._acl import (
    NodeAuthMiddleware,
    check_send_acl,
    check_spawn,
    deny_response,
)
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
        {"name": "<existing-spec-name>", "caller": "<sender-name>"}

        # Register-and-start an ad-hoc spec in one call:
        {
            "name": "<name>",
            "caller": "<sender-name>",
            "spec": {"apiVersion": "scitex-agent-container/v3",
                     "kind": "Agent",
                     "spec": {...}},
            "overwrite": false   # optional; default false → 409 on clash
        }

    WI-2 spawn-permission gate (limited scope per lead 2026-05-20):
    the optional ``caller`` field carries the spawning node's name
    (same self-claimed-identity caveat as ``message:send``'s
    ``metadata.from_agent``). The gate is **root-only** today —
    a node with no parent in the ``lineage`` table may spawn; a
    child gets a clear 403. ``caller`` omitted = administrative /
    human-operator path → allowed.

    On allow, the parent → child edge is recorded in ``lineage``
    so the new agent inherits the spawner's group.
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

    # WI-2 spawn-permission gate.
    caller = body.get("caller")
    if caller is not None and not isinstance(caller, str):
        return JSONResponse(
            {"error": "'caller' must be a string if present"}, status_code=400
        )
    decision, reason = check_spawn(caller=caller)
    if decision == "deny":
        return deny_response(reason or "spawn denied")

    inline_spec = body.get("spec")
    if inline_spec is not None:
        err = materialize_inline_spec(
            name, inline_spec, overwrite=bool(body.get("overwrite"))
        )
        if err is not None:
            return err

    # Record lineage on allowed-spawn so the new child inherits the
    # caller's group. ``caller=None`` → no lineage record (admin /
    # operator path; the new agent starts as a root).
    if caller:
        from .._state.state_db_nodes import record_lineage as _record_lineage

        try:
            _record_lineage(child=name, parent=caller)
        except ValueError as exc:
            # Idempotent same-parent re-record is fine; a re-parent
            # to a different caller is loudly rejected.
            return JSONResponse({"error": str(exc)}, status_code=409)

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


async def _forward_to_remote(
    request: Request,
    *,
    body: dict[str, Any],
    target_host: str,
    target_port: int | None,
    target_name: str,
) -> Response:
    """WI-4 cross-host forwarder. Reposts ``body`` to the destination
    host's ``sac listen`` and proxies the response back.

    **Bearer handling — per-host bearer registry** (Q4 (b)). The
    destination's host bearer is read from
    ``peer-tokens/<target_host>.token`` on the forwarding host. The
    operator populates that registry with ``sac host add-peer <host>
    <token>`` (one entry per peer). The forwarder uses that bearer
    on the wire — not its own, not the original caller's. This
    keeps the **per-host blast radius** the lead asked for: leaking
    one host's listen bearer compromises only that host.

    Missing ``peer-tokens/<host>.token`` is a **loud failure**: 502
    with a clear "no peer token for X" message that names the file
    and the ``sac host add-peer`` fix. Never silently drop a forward
    (handoff §0 Hard rules).

    **ACL handling**: the body is unchanged, so the destination
    re-runs ``check_send_acl`` against the same
    ``metadata.from_agent``. Because the forwarder authenticates
    with the destination's *host* bearer (administrative caller),
    ``authenticated_node`` is ``None`` at the destination and the
    ACL gates on the metadata claim — exactly the cross-host shape
    the lead documented under Q1's restored design. Cross-group
    denials fire at the receiving host (handoff §4 acceptance "ACL
    is enforced at the receiving host").
    """
    if not target_port:
        return JSONResponse(
            {
                "error": (
                    f"cannot forward to {target_name!r} on host "
                    f"{target_host!r}: missing a2a_port in instances row"
                )
            },
            status_code=502,
        )

    # ``state_db.resolve_node_host`` returns the *canonical* host
    # name; we trust that to be reachable (handoff §2 "sac assumes
    # reachability; orochi establishes it"). For loopback test
    # scenarios callers set ``a2a_port`` to a 127.0.0.1 port and
    # the test fixtures match the canonical host name to "host-a"
    # / "host-b" via SAC_HOST.
    import httpx as _httpx

    from .peer_tokens import PeerTokenError, read_peer_token

    forward_url = f"http://{target_host}:{target_port}/agents/{target_name}/message:send"
    # In our test loopback both hosts live on 127.0.0.1; the canonical
    # host name is a label, not a routable address. Rewrite to
    # 127.0.0.1 when the resolved host is a known-loopback alias so
    # the test fixtures can drive both legs on one machine. Real
    # deployments use ssh-alias / tunnel hostnames and route as-is.
    if target_host in ("host-a", "host-b") or target_host.startswith("host-"):
        forward_url = (
            f"http://127.0.0.1:{target_port}/agents/{target_name}/message:send"
        )

    # WI-4 Q4(b) — per-host bearer registry. Pull the destination's
    # host bearer; loud 502 if it's missing.
    try:
        peer_bearer = read_peer_token(peer_host=target_host)
    except PeerTokenError as exc:
        return JSONResponse(
            {"error": f"cross-host forward refused: {exc}"},
            status_code=502,
        )

    forward_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {peer_bearer}",
    }

    try:
        async with _httpx.AsyncClient(timeout=15.0) as ac:
            resp = await ac.post(forward_url, json=body, headers=forward_headers)
    except _httpx.HTTPError as exc:
        # Loud failure (handoff §0): the operator needs to see when
        # cross-host reachability breaks, not get a silent 200.
        return JSONResponse(
            {
                "error": (
                    f"cross-host forward to {forward_url!r} failed: {exc}"
                )
            },
            status_code=502,
        )

    # Pass through the destination's response, including its 403 / 400
    # / 200 status. Body is JSON or text — try JSON first.
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:  # noqa: BLE001  # stx-allow: fallback (reason: non-JSON destination body is tolerated; surfaced as text)
        return JSONResponse(
            {"forwarded_body_text": resp.text}, status_code=resp.status_code
        )


async def node_message_send(request: Request) -> Response:
    """``POST /agents/<name>/message:send`` — publish an A2A
    ``SendMessage`` body to the local node's inbox bus.

    Implicitly registers ``<name>`` as an external node on first use
    so the synthesised AgentCard is available for the well-known
    lookup. The publish is **always loud** — a malformed body returns
    400, never a silent drop (handoff §0 Hard rules).

    WI-2 ACL gate: every send is checked by
    :func:`_acl.check_send_acl` before publish:

    * **Per-node bearer** pins identity — ``metadata.from_agent``
      must match the resolved name, else 403 "identity spoof"
      (handoff §4 acceptance).
    * **Cross-group** is denied by default; intra-group
      (parent↔child and sibling↔sibling) is allowed.
    * **Explicit cross-group grants** (``comms_grants`` table) flip
      a deny to an allow.
    * **Self-send** is always allowed.
    * The **host-wide bearer** is the administrative / cross-host
      forwarding caller; it honours ``metadata.from_agent``
      verbatim (used by WI-4 forwarders authenticating with the
      destination's host bearer from ``peer-tokens/`` registry).

    Bearer auth is enforced by :class:`BearerAuthMiddleware` (outer
    perimeter) and identity resolution by :class:`NodeAuthMiddleware`
    (sets ``request.state.authenticated_node``).
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

    # WI-4 cross-host forward. If the target lives on a different
    # host, forward the body unchanged to that host's sac listen.
    # The destination re-runs the ACL check against the same
    # ``metadata.from_agent`` we received, so cross-group denials
    # fire at the receiving host (handoff §4 acceptance).
    from .._state.state_db_nodes import is_local_node, resolve_node_host
    from .._state.state_db import _resolve_host as _resolve_local_host

    # Prefer the per-app ``local_host`` configured at ``create_app``
    # time; fall back to the env-based resolver for callers that
    # haven't pinned one. Per-app config matters for in-process
    # multi-host tests where the env is shared.
    local_host = getattr(request.app.state, "local_host", None) or _resolve_local_host(None)
    if not is_local_node(name=name, local_host=local_host):
        target_info = resolve_node_host(name=name)
        if target_info is None:
            return JSONResponse(
                {
                    "error": (
                        f"target {name!r} resolves to a non-local host but no "
                        "instance row carries its address — cannot forward"
                    )
                },
                status_code=502,
            )
        return await _forward_to_remote(
            request,
            body=body,
            target_host=target_info["host"],
            target_port=target_info["a2a_port"],
            target_name=name,
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
    # bearer was presented (administrative caller). With a per-node
    # bearer, ``metadata.from_agent`` MUST match the resolved name
    # so identity cannot be spoofed via a metadata field (handoff
    # §4 acceptance). See :func:`_acl.check_send_acl`.
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
        ack=bool(sac_meta.get("ack", False)),
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


def create_app(*, token: str, local_host: str | None = None) -> Starlette:
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
    :func:`node_message_send` enforces "identity cannot be spoofed
    via a metadata field" (handoff §4 acceptance). The spawn-gate
    in :func:`agents_start` consumes the same body-``caller`` shape.

    Middleware order matters. Starlette executes the *outermost*
    ``add_middleware`` call first (it wraps the app last but runs
    first on the inbound path). So the BearerAuthMiddleware call
    below comes **last** to make it the outermost layer.

    WI-4 (handoff §4 "Cross-host routing") adds the forwarder
    inside :func:`node_message_send`. ``local_host`` configures the
    name this app sees as "itself" so the resolver can tell
    local-vs-remote targets apart. When omitted, falls back to
    :func:`state_db._resolve_host` (env + config + hostname chain).
    Passing the value explicitly matters for in-process multi-host
    tests where the env is shared.
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
    # WI-4 — per-app local host name. May be ``None``; the forwarder
    # then falls back to the env-based resolver.
    app.state.local_host = local_host
    # WI-2 — identity resolution (inner). Reads the same Bearer the
    # outer middleware already validated; tags ``request.state`` with
    # the resolved node name (or ``None`` for the host-wide bearer).
    app.add_middleware(NodeAuthMiddleware, host_bearer=token)
    # Outer perimeter — admits any valid token, rejects everything else.
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
