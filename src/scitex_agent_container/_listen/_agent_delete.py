"""``DELETE /agents/<name>`` handler for ``sac listen`` (extracted from server.py).

Hosts the agent-lifecycle DELETE responsibility — stop a running agent
(SIGTERM), or surface the stillborn / not-found / ACL-deny cases with
the right status code. Split out of
:mod:`scitex_agent_container._listen.server` (which grew past the
per-file line cap). ``server.py`` re-imports :func:`agent_delete` so
route registration (:func:`_v1_agent_routes`) and the historical
``from ..._listen.server import agent_delete`` import path keep working
unchanged. No behaviour change — this is a pure extraction of one
cohesive responsibility (agent-lifecycle delete), mirroring the existing
``_node_channel`` / ``_agent_exec`` extractions.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._runners._session_state import state_dir_for

__all__ = ["agent_delete"]


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
        permission to operate on this target. Nothing sets that
        attribute since 2026-08-28, so this branch is unreachable over
        HTTP today; see the gate below. Body shape:
        ``{"error": "ACL deny", "kind": "acl_deny", "reason": "..."}``
        — the 5th kind in the wire taxonomy pinned with clew.

    Splitting 410 from 404 is the operator-actionable difference:
    "never existed" vs. "existed, has been removed". Splitting 403
    from both is identity-actionable: the agent exists (or doesn't,
    irrelevant) but the caller can't touch it.
    """
    name = request.path_params["name"]
    # PR-3 — lineage-scoped ACL gate. ``authenticated_node`` was the
    # resolved per-node identity; the middleware that set it was removed
    # 2026-08-28 (nothing ever minted a per-node bearer, so it was always
    # ``None`` in any case), and this route reads no other caller shape —
    # no body, no query param. So ``caller`` is ``None`` on every request
    # and ``check_lineage_acl`` admits it as administrative. Stated plainly
    # because the alternative is a reader assuming DELETE is lineage-gated
    # against a non-admin caller that has never existed. The gate is kept:
    # it costs nothing, and it is where a real caller identity would land.
    # It still runs BEFORE we touch the state dir / pid file so a denied
    # caller learns nothing about whether the target exists.
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
            from .._lifecycle._startup_failed_supersede import liveness_since_failure

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
            refuted_by = liveness_since_failure(sd, marker, name=name)
            if refuted_by:
                body["startup_failed_superseded_by"] = refuted_by
            return JSONResponse(body, status_code=410)
        return JSONResponse({"error": "no pid file"}, status_code=404)
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"name": name, "stopped": True, "pid": pid})
