"""Cross-host forwarders for ``sac listen`` (extracted from ``_node_channel``).

Holds the WI-4 cross-host forward path that the node-comms routes in
:mod:`._node_channel` invoke when the target node lives on a different
host. Three callables make up the surface:

* :func:`_forward_to_remote` — entry point + transport selector. Reads
  the per-host bearer registry and the operator's ``peers:`` block,
  then dispatches to the HTTP or ssh leg.
* :func:`_forward_via_http` — legacy HTTP transport (Stage 1). Used
  when the destination host is NOT in ``host_config.peers``; matches
  the two-listen test topology's loopback rewrite.
* :func:`_forward_via_ssh_curl` — ADR-0015 Stage 2 transport. ssh +
  remote curl into ``127.0.0.1:<port>`` on the destination, reusing
  the same helper as the ``/v1/turn`` direct-ssh path.

The split keeps the route handlers in :mod:`._node_channel` under the
per-file LOC cap; ``_node_channel`` re-exports
:func:`_forward_to_remote` so the existing ``from ._listen._node_channel
import _forward_to_remote`` import path keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

from starlette.responses import JSONResponse, Response

__all__ = [
    "_forward_to_remote",
    "_forward_via_http",
    "_forward_via_ssh_curl",
]


async def _forward_to_remote(
    request: Any,
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
    on the wire — not its own, not the original caller's.

    Missing ``peer-tokens/<host>.token`` is a **loud failure**: 502
    with a clear "no peer token for X" message that names the file
    and the ``sac host add-peer`` fix. Never silently drop a forward
    (handoff §0 Hard rules).

    **ACL handling**: the body is unchanged across transports, so
    the destination re-runs ``check_send_acl`` against the same
    ``metadata.from_agent``. Because the forwarder authenticates
    with the destination's *host* bearer (administrative caller),
    ``authenticated_node`` is ``None`` at the destination and the
    ACL gates on the metadata claim. Cross-group denials fire at
    the receiving host (handoff §4 acceptance "ACL is enforced at
    the receiving host").

    **Transport selector (ADR-0015 Stage 2)**: when ``target_host``
    is a member of ``host_config.peers`` (including via glob keys
    like ``spartan-*``), the forward leg is ssh + remote curl
    instead of plain HTTP — a WAN hostname like ``ywata-note-win``
    is rarely routable directly between hosts, but the operator's
    existing ssh trust to that host is. Hosts NOT in ``peers:`` keep
    the legacy HTTP path verbatim, including the ``host-*`` loopback
    alias rewrite the two-listen tests depend on.
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

    from .peer_tokens import PeerTokenError, read_peer_token

    try:
        peer_bearer = read_peer_token(peer_host=target_host)
    except PeerTokenError as exc:
        return JSONResponse(
            {"error": f"cross-host forward refused: {exc}"},
            status_code=502,
        )

    # ADR-0015 transport selector. Resolve ``target_host`` against the
    # operator's ``peers:`` block (with glob fallback via PeersMap). If
    # the destination is a known peer, ssh-tunnel the POST; otherwise
    # fall through to the legacy HTTP path.
    from .._state.host_config import load as _load_host_config

    try:
        _cfg = _load_host_config()
        peer_spec = _cfg.peers.get(target_host)
    except Exception:  # noqa: BLE001  # stx-allow: fallback (reason: a malformed config.yaml must not silently disable the HTTP fallback)
        peer_spec = None

    if peer_spec is not None and peer_spec.ssh:
        return await _forward_via_ssh_curl(
            target_host=target_host,
            target_port=target_port,
            target_name=target_name,
            body=body,
            peer_bearer=peer_bearer,
            ssh_target=peer_spec.ssh,
        )

    return await _forward_via_http(
        target_host=target_host,
        target_port=target_port,
        target_name=target_name,
        body=body,
        peer_bearer=peer_bearer,
    )


async def _forward_via_http(
    *,
    target_host: str,
    target_port: int,
    target_name: str,
    body: dict[str, Any],
    peer_bearer: str,
) -> Response:
    """Legacy HTTP-only cross-host forward (Stage 1). Used when
    ``target_host`` is NOT in ``host_config.peers`` — typically the
    in-process two-listen test topology, or a deployment where overlay
    routing makes the canonical host name directly reachable.
    """
    import httpx as _httpx

    forward_url = (
        f"http://{target_host}:{target_port}/agents/{target_name}/message:send"
    )
    # In our test loopback both hosts live on 127.0.0.1; the canonical
    # host name is a label, not a routable address. Rewrite to
    # 127.0.0.1 when the resolved host is a known-loopback alias so
    # the test fixtures can drive both legs on one machine. Real
    # deployments use ssh-alias / tunnel hostnames and route as-is.
    if target_host in ("host-a", "host-b") or target_host.startswith("host-"):
        forward_url = (
            f"http://127.0.0.1:{target_port}/agents/{target_name}/message:send"
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
            {"error": (f"cross-host forward to {forward_url!r} failed: {exc}")},
            status_code=502,
        )

    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:  # noqa: BLE001  # stx-allow: fallback (reason: non-JSON destination body is tolerated; surfaced as text)
        return JSONResponse(
            {"forwarded_body_text": resp.text}, status_code=resp.status_code
        )


async def _forward_via_ssh_curl(
    *,
    target_host: str,
    target_port: int,
    target_name: str,
    body: dict[str, Any],
    peer_bearer: str,
    ssh_target: str,
) -> Response:
    """ADR-0015 Stage 2 transport: ssh into ``ssh_target`` and curl-POST
    to ``127.0.0.1:target_port`` on the remote.

    The remote curl carries the destination's
    ``peer-tokens/<target_host>.token`` bearer, so the destination's
    :class:`NodeAuthMiddleware` admits the request as an administrative
    caller (same shape as the HTTP path). Cross-group ACL denials fire
    at the destination, not here.

    The ssh leg runs in a worker thread so the event loop is not
    blocked by ``subprocess.run`` — ``asyncio.to_thread`` keeps the
    request's other tasks (the local SSE subscribers' broker fan-out,
    the ``request.is_disconnected`` poll) responsive while the ssh
    transport completes.
    """
    from .._network._ssh_curl import _post_via_ssh_curl

    body_bytes = _json.dumps(body).encode("utf-8")
    path = f"/agents/{target_name}/message:send"

    try:
        rc, stdout, stderr = await asyncio.to_thread(
            _post_via_ssh_curl,
            host=ssh_target,
            port=target_port,
            path=path,
            body=body_bytes,
            bearer=peer_bearer,
            timeout_s=15.0,
        )
    except ValueError as exc:
        return JSONResponse(
            {"error": f"cross-host forward refused: {exc}"},
            status_code=502,
        )

    if rc != 0:
        # ssh-level / curl-level failure surfaces as a 502 with the
        # same shape as the HTTP path so the operator UX is uniform
        # across transports.
        err_tail = stderr.decode("utf-8", errors="replace").strip()[:300]
        return JSONResponse(
            {
                "error": (
                    f"cross-host forward to ssh://{ssh_target} "
                    f"(→127.0.0.1:{target_port}{path}) failed "
                    f"(rc={rc}): {err_tail}"
                )
            },
            status_code=502,
        )

    # Curl over a healthy ssh connection prints the response body to
    # stdout; HTTP status is invisible at this layer (curl wasn't asked
    # for ``-w '%{http_code}'``). Mirror the HTTP path: try to parse
    # JSON, else surface as text. The destination returns 200 on
    # success and 403 with a JSON body on deny — both shapes parse.
    stdout_text = stdout.decode("utf-8", errors="replace")
    # Take the last non-empty line in case the remote shell printed
    # banners before curl's body — same defensive parsing as the
    # ``/v1/turn`` ssh path.
    lines = [line for line in stdout_text.strip().splitlines() if line.strip()]
    if not lines:
        return JSONResponse({"forwarded_body_text": stdout_text}, status_code=200)
    try:
        payload = _json.loads(lines[-1])
    except _json.JSONDecodeError:
        return JSONResponse({"forwarded_body_text": stdout_text}, status_code=200)
    # Heuristic: when the destination's ACL denies the send, the body
    # contains an ``error`` key naming the deny reason; surface that as
    # 403 so the originating sender sees the same status as on the
    # HTTP path. Success bodies do not carry ``error``.
    if isinstance(payload, dict) and "error" in payload:
        status = 403 if "ACL" in str(payload.get("error", "")) else 502
        return JSONResponse(payload, status_code=status)
    return JSONResponse(payload, status_code=200)
