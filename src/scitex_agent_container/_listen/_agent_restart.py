"""``POST /agents/<name>/restart`` handler for ``sac listen``.

The container-side mirror of the spawn bypass (:mod:`._agent_exec`
``agents_start``), but for the *restart* lifecycle verb: an agent
running INSIDE a SIF cannot resolve a peer's LOCAL registry row +
local ``state.db`` (``sac agents restart`` hard-fails with "not found
in registry" from inside the container). This handler lets the
container ask the HOST listen to run the restart on the bare host —
where the registry row, the agent's runtime dir, and that node's
``sac listen`` token actually live.

Shape mirrors ``agents_start`` exactly:

  * resolve the caller identity from ``request.state.authenticated_node``
    (the per-node bearer resolved by :class:`._acl.NodeAuthMiddleware`)
    with a body-``caller`` fallback for the host-bearer / admin path,
  * run :func:`._acl.check_lineage_acl` (the MANAGE gate — self /
    lineage-descendant / developer-group / standard-fleet mesh) BEFORE
    any runtime work; deny → 403 with the ACL reason verbatim,
  * on allow, shell the canonical ``sac agents restart <name> --yes
    --json`` on the bare host (same host-shell path ``agents_start``
    uses for ``sac agents start``) and return its rc + stdout + stderr
    as JSON. A non-zero rc is surfaced as 502 (the gate passed but the
    bare-host restart itself failed), never swallowed.
"""

from __future__ import annotations

import asyncio
import os
import shutil

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._acl import check_lineage_acl, deny_response

__all__ = ["agent_restart"]


async def agent_restart(request: Request) -> JSONResponse:
    """POST /agents/<name>/restart — restart the named agent on the host.

    The container-side mirror of the spawn bypass: the in-SIF
    :mod:`._lifecycle._restart_client` POSTs here so the restart runs
    on the bare host (where the registry row + runtime dir live)
    instead of failing "not found in registry" inside the container.

    Identity + ACL (mirrors ``agents_start`` / ``agent_delete``):

      * ``request.state.authenticated_node`` is the resolved per-node
        identity from :class:`._acl.NodeAuthMiddleware`; ``None`` is
        the host-wide bearer (administrative / operator path — always
        allowed by :func:`check_lineage_acl`).
      * When the host-wide bearer is used, an optional body ``caller``
        field carries the requesting node's name (same self-claimed
        caveat as ``agents_start``'s ``caller``) so the manage gate can
        still apply the standard-fleet mesh for an admin-forwarded
        request. A per-node bearer always wins over the body claim.
      * :func:`check_lineage_acl` is the MANAGE gate: self /
        lineage-descendant / developer-group / standard-fleet mesh.
        Deny → 403 + ``{"error","kind":"acl_deny","reason"}``.

    On allow, shell ``sac agents restart <name> --yes --json`` on the
    bare host and return its rc + stdout + stderr. rc != 0 → 502.
    """
    name = request.path_params["name"]

    # Body is optional (the client may POST an empty body when it relies
    # on a per-node bearer for identity). A malformed body is tolerated:
    # the per-node bearer is the authoritative identity source.
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (reason: empty/malformed body → no caller claim; identity falls back to the per-node bearer below)
        body = {}
    if not isinstance(body, dict):
        body = {}
    claimed_caller = body.get("caller")
    if claimed_caller is not None and not isinstance(claimed_caller, str):
        return JSONResponse(
            {"error": "'caller' must be a string if present"}, status_code=400
        )

    # Resolve the effective caller for the MANAGE gate. A per-node bearer
    # (authenticated_node) is the authoritative, unspoofable identity; the
    # body ``caller`` claim is only consulted on the host-wide bearer path
    # (authenticated_node is None) so an administrative forwarder can name
    # the on-behalf-of node for the standard-fleet mesh.
    authenticated = getattr(request.state, "authenticated_node", None)
    caller = authenticated if authenticated is not None else claimed_caller

    decision, reason = check_lineage_acl(caller=caller, target=name)
    if decision == "deny":
        return deny_response(reason or "lineage ACL deny")

    # ``fresh`` (optional): start a NEW Claude session instead of a resuming
    # restart — the deterministic recovery for an agent wedged on a boot prompt
    # whose queued-input buffer returns on every plain restart. Default (absent
    # / falsey) keeps the byte-identical plain-restart argv below.
    fresh = bool(body.get("fresh"))

    sac_bin = shutil.which("sac") or "sac"
    # ``agents`` (plural) is the canonical group; ``--yes`` skips the
    # interactive guard (this POST IS the confirmation) and ``--json``
    # gives a parseable envelope — exactly the argv ``agent_restart`` MCP
    # tool + cross-host dispatch already run. Strip the in-SIF env markers
    # so a listen running inside a parent SIF doesn't re-broker the child
    # restart back to itself (same recursion guard as ``agents_start``).
    child_env = dict(os.environ)
    child_env.pop("APPTAINER_CONTAINER", None)
    child_env.pop("SINGULARITY_CONTAINER", None)
    if fresh:
        # New session, no resume: stop-then-start fresh. ``start`` accepts
        # --force (stop if running), --fresh (never --continue) and --json.
        inner_argv = [sac_bin, "agents", "start", name, "--force", "--fresh", "--json"]
    else:
        inner_argv = [sac_bin, "agents", "restart", name, "--yes", "--json"]

    proc = await asyncio.create_subprocess_exec(
        *inner_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace") if out else ""
    stderr = err.decode("utf-8", errors="replace") if err else ""
    returncode = proc.returncode if proc.returncode is not None else -1

    payload = {
        "name": name,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if returncode != 0:
        # The gate passed but the bare-host restart itself failed (e.g.
        # the agent has no registry row AND no resolvable spec). Surface
        # it as 502 with the rc + stderr — never a fake success.
        return JSONResponse(payload, status_code=502)
    return JSONResponse(payload, status_code=200)
