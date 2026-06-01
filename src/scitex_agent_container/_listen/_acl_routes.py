"""ACL decision routes for ``sac listen`` (task #27 PR B).

The bare host's lead runs ``sac a2a {unblock,block,grant}`` directly
and writes the host's runtime/state.db (correct). An IN-CONTAINER
agent that runs the same CLI today writes ITS OWN per-container
state.db — silently ineffective against the host listen's ACL
checks (lead FUTURE item 4 / operator greenlit Q5).

These routes give the in-container CLI a way to TARGET the host
listen's state.db over HTTP, mirroring the SAC-from-SAC
``_lifecycle._spawn_client`` → host ``POST /agents`` broker
pattern from #261. The CLI detects in-SIF and POSTs here; on the
bare host it skips the broker entirely.

Endpoints (loopback-only, bearer-auth):

    POST /v1/acl/unblock   body {"sender", "target", "note"?}
    POST /v1/acl/block     body {"sender", "target", "note"?}
    POST /v1/acl/grant     body {"sender", "target", "note"?}     (= unblock alias)

Each handler calls the same DB-only helper the CLI's bare-host
path uses (``grant_flush.unblock_and_clear_pending`` /
``block_and_clear_pending``), so a future operator who reads the
server log sees the same operation either way.

The handlers are AUTHORITATIVE: the listen-server's ``state.db`` is
where the source-of-truth ACL writes land. There is no
sender-identity check on the body — the operator that runs the
CLI is implicitly trusted by the bearer (the bearer is host-wide).
A future PR could add per-token grant authority if needed.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = [
    "acl_block",
    "acl_grant",
    "acl_unblock",
]


async def _body_json(request: Request) -> dict | None:
    try:
        return await request.json()
    except Exception:  # stx-allow: fallback (reason: malformed JSON → 400)
        return None


def _extract_pair(body: dict) -> tuple[str, str, str | None] | JSONResponse:
    """Pull ``sender`` + ``target`` (+ optional ``note``) from the body.

    Returns either ``(sender, target, note)`` on success or a
    :class:`JSONResponse` with status 400 + a clear reason on
    invalid input. Inputs MUST be non-empty strings — empty is
    rejected loudly, never coerced.
    """
    sender = body.get("sender") if isinstance(body, dict) else None
    target = body.get("target") if isinstance(body, dict) else None
    note = body.get("note") if isinstance(body, dict) else None
    if not isinstance(sender, str) or not sender:
        return JSONResponse(
            {"error": "missing or empty 'sender' string"}, status_code=400
        )
    if not isinstance(target, str) or not target:
        return JSONResponse(
            {"error": "missing or empty 'target' string"}, status_code=400
        )
    if note is not None and not isinstance(note, str):
        return JSONResponse(
            {"error": "'note' must be a string if present"}, status_code=400
        )
    return sender, target, note


async def acl_unblock(request: Request) -> JSONResponse:
    """``POST /v1/acl/unblock`` — write grant + clear block + clear pending."""
    body = await _body_json(request)
    if body is None:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    parsed = _extract_pair(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    sender, target, note = parsed
    from .._state.grant_flush import unblock_and_clear_pending

    try:
        result = unblock_and_clear_pending(sender=sender, target=target, note=note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def acl_block(request: Request) -> JSONResponse:
    """``POST /v1/acl/block`` — write block + clear pending."""
    body = await _body_json(request)
    if body is None:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    parsed = _extract_pair(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    sender, target, note = parsed
    from .._state.grant_flush import block_and_clear_pending

    try:
        result = block_and_clear_pending(sender=sender, target=target, note=note)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def acl_grant(request: Request) -> JSONResponse:
    """``POST /v1/acl/grant`` — alias of ``/v1/acl/unblock`` for back-compat.

    Body shape identical. Kept so existing scripts that POST to
    ``/v1/acl/grant`` keep working — the receiver-driven framing
    introduced in task #27 prefers ``unblock``, but the legacy
    name remains valid.
    """
    return await acl_unblock(request)
