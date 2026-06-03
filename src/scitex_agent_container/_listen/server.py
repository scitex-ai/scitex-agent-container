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

import os
import urllib.error as _urlerror
import urllib.request as _urlrequest
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .._runners._session_state import read_session_id, state_dir_for
from .._state.registry import Registry
from ..config import load_config
from ..config._resolve import resolve_config
from ._acl import (
    NodeAuthMiddleware,
)
from ._nodes import Broker, NodeRegistry
from .auth import BearerAuthMiddleware

# Re-exported under the module's public surface so unit tests can patch
# them as ``scitex_agent_container._listen.server._urlrequest.urlopen``
# / ``._urlerror.URLError`` without forcing every call site to alias.
__all__ = ["create_app", "_urlrequest", "_urlerror"]


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
    body: dict[str, Any] = {
        "name": name,
        "spec_path": str(spec_path),
        "workdir": cfg.expanded_workdir,
        "session_id": sid,
        "state_dir": str(sd),
    }
    # PR-1 — stillborn surface. If the runtime dir has a
    # ``STARTUP_FAILED`` marker (= the spawn never produced an SDK
    # session), echo it so callers don't have to also poll a separate
    # endpoint to know why ``session_id`` is null. Matches the 410
    # body shape returned by DELETE on the same condition.
    from .._lifecycle._startup_failed import read_marker

    marker = read_marker(sd)
    if marker is not None:
        body["status"] = "startup_failed"
        body["startup_failed"] = marker
    return JSONResponse(body)


# --- extracted handlers re-imported for routes + back-compat ---------------
#
# ``agent_send`` (prompt / interrupt-key) + ``agents_start`` (claude-exec),
# the ``agent_tail`` SSE-over-session.jsonl handler, and the node inbox
# channel were all extracted into focused sibling modules (server.py hit the
# per-file line cap). Re-imported here so route registration
# (:func:`_v1_agent_routes`) and the historical
# ``from ..._listen.server import agent_send`` test import path keep working
# unchanged.
from ._agent_exec import (  # noqa: E402
    _find_claude_binary,  # noqa: F401  (re-exported for tests)
    agent_send,
    agents_start,
)
from ._tail import agent_tail  # noqa: F401, E402  (re-exported for routes)


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

    return JSONResponse({"error": f"unknown agent or node: {name!r}"}, status_code=404)


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
# container.
#
# Routes registered in ``_v1_agent_routes`` below:
#
#   POST /agents/{name}/message:send  → node_message_send
#   GET  /agents/{name}/inbox/stream  → node_inbox_stream
#
# The handlers were extracted into ``_node_channel`` (server.py hit the
# per-file line cap); re-imported here so route registration and the
# historical ``from ..._listen.server import node_message_send`` import
# path keep working unchanged. ``_forward_to_remote`` rides along (it is
# the cross-host leg of ``node_message_send``).
#
# The agent-card route is *not* overridden here — instead
# :func:`agent_card` falls back to the synthesised card for nodes
# that are not YAML-backed. That fall-back is added to the existing
# handler below.
from ._node_channel import (  # noqa: E402
    _forward_to_remote,  # noqa: F401  (re-exported for tests)
    node_inbox_stream,
    node_message_send,
)


async def agent_delete(request: Request) -> JSONResponse:
    """DELETE /agents/<name> — stop the agent.

    Four cases distinguished by the response code:

      * **200 OK** — agent is live; ``pid`` file present; SIGTERM sent.
      * **410 Gone** (PR-1) — agent is *stillborn*: a ``STARTUP_FAILED``
        marker is on disk (set by the POST /agents handler on a
        non-zero ``sac agents start`` exit). The body carries the
        failure details so the caller doesn't need to also
        ``GET .../status``.
      * **404 Not Found** — agent never existed or was already deleted.
      * **403 Forbidden** (PR-3) — caller is identified (via
        ``request.state.authenticated_node``) but lacks lineage-scoped
        permission to operate on this target. Body shape:
        ``{"error": "ACL deny", "kind": "acl_deny", "reason": "..."}``
        — the 5th kind in the wire taxonomy pinned with clew.

    Splitting 410 from 404 is the operator-actionable difference:
    "never existed" vs. "existed, has been removed". Splitting 403
    from both is identity-actionable: the agent exists (or doesn't,
    irrelevant) but the caller can't touch it.
    """
    name = request.path_params["name"]
    # PR-3 — lineage-scoped ACL gate. ``authenticated_node`` is the
    # resolved per-node identity from NodeAuthMiddleware; ``None``
    # is the administrative / host-wide bearer (always allowed by
    # check_lineage_acl). The ACL is enforced BEFORE we touch the
    # state dir / pid file so a denied caller learns nothing about
    # whether the target exists (status, marker, runtime files).
    from ._acl import check_lineage_acl, deny_response

    caller = getattr(request.state, "authenticated_node", None)
    decision, reason = check_lineage_acl(caller=caller, target=name)
    if decision == "deny":
        return deny_response(reason or "lineage ACL deny")
    sd = state_dir_for(name)
    pid_file = sd / "pid"
    if not pid_file.is_file():
        # PR-1 — distinguish stillborn (have STARTUP_FAILED marker) from
        # genuinely not-found. Stillborn → 410 Gone + the structured
        # failure body the operator/orchestrator can branch on without
        # also hitting GET /agents/<name>/status.
        #
        # Wire shape per clew review (#287):
        #
        # The "headline" failure fields (status, phase, kind, failed_at,
        # runtime_dir, remediation_hint) are LIFTED to the top level so a
        # clew-launcher error renderer can branch / display without
        # walking into ``details``. ``see_also`` is the host-absolute
        # path to the on-disk marker so a human / sysadmin can ``cat``
        # the marker (and the peer ``stdout.log`` / ``stderr.log`` in
        # the same directory) without recomputing it. The full marker
        # remains under ``details`` for parity with the marker file
        # contents (and so an orchestrator can hash it for dedupe).
        from .._lifecycle._startup_failed import MARKER_FILENAME, read_marker

        marker = read_marker(sd)
        if marker is not None:
            runtime_dir = marker.get("runtime_dir", str(sd.resolve()))
            body: dict[str, Any] = {
                "name": name,
                "status": "startup_failed",
                "kind": marker.get("kind"),
                "phase": marker.get("phase"),
                "failed_at": marker.get("failed_at"),
                "runtime_dir": runtime_dir,
                "remediation_hint": marker.get("remediation_hint", ""),
                "see_also": f"{runtime_dir}/{MARKER_FILENAME}",
                "details": marker,
            }
            return JSONResponse(body, status_code=410)
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
    # Task #27 PR B — ACL decision routes for the in-container
    # broker. The bare-host lead writes the host's state.db directly
    # via the CLI; an in-container ``sac a2a {unblock,block,grant}``
    # posts here so the writes land on the HOST listen's state.db
    # (rather than the silently-ineffective per-container copy).
    from ._acl_routes import acl_block, acl_grant, acl_unblock

    routes: list[Route] = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/.well-known/agent-card.json", fleet_card_handler, methods=["GET"]),
        Route("/v1/acl/unblock", acl_unblock, methods=["POST"]),
        Route("/v1/acl/block", acl_block, methods=["POST"]),
        Route("/v1/acl/grant", acl_grant, methods=["POST"]),
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
