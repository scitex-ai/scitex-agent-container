"""Cross-host forwarders for ``sac listen`` (extracted from ``_node_channel``).

Holds the WI-4 cross-host forward path that the node-comms routes in
:mod:`._node_channel` invoke when the target node lives on a different
host. Three callables make up the surface:

* :func:`_forward_to_remote` — entry point + transport selector. Reads
  the per-host bearer registry, resolves the destination through the
  SAME peer map the CLI verbs use (config.yaml ``peers:`` UNION the
  scitex-dev host registry — :func:`~.._state._peer_resolve.peers_with_registry`),
  and dispatches to the ssh leg. A destination that is not an ssh peer
  is REFUSED with a 502 that names the fix; it is never guessed at.
* :func:`_forward_via_ssh_curl` — ADR-0015 Stage 2 transport, the ONLY
  production transport. ssh + remote curl into ``127.0.0.1:<port>`` on
  the destination, reusing the same helper as the ``/v1/turn``
  direct-ssh path.
* :func:`_forward_via_http` — the in-process TEST transport. Reached
  only for the ``host-*`` loopback aliases the two-listen suite in
  ``test_server.py`` stamps on its ``instances`` rows
  (:func:`_is_test_loopback_alias`); never for a fleet host.

Why there is no HTTP leg for production (measured 2026-09-02)
--------------------------------------------------------------
Every agent's bridge and sidecar bind ``127.0.0.1`` by default
(``runtimes/_tui_turn_bridge_lifecycle.py`` ``DEFAULT_HOST``,
``runtimes/a2a_sidecar.py`` host default) and no spec on the fleet
declares ``spec.a2a.host``, so ``http://<host>:<agent-port>/...`` can
never connect from another machine. This module used to take that leg
whenever the destination was absent from the RAW ``peers:`` block of
``~/.scitex/agent-container/config.yaml`` — and ``scitex-compute-01`` /
``scitex-compute-03`` have no such file at all, so every cross-host send
from those hosts silently posted plain HTTP and died with ``All
connection attempts failed``. The CLI verbs (``sac host probe`` and
friends) had already stopped gating on the raw block and resolve peers
through the host registry's ``ssh_alias``; the forwarder simply was not
using the same SSoT. Now it does, and the only fallback is a loud one.

The split keeps the route handlers in :mod:`._node_channel` under the
per-file LOC cap; ``_node_channel`` re-exports
:func:`_forward_to_remote` so the existing ``from ._listen._node_channel
import _forward_to_remote`` import path keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse, Response

__all__ = [
    "_forward_to_remote",
    "_forward_via_http",
    "_forward_via_ssh_curl",
    "_is_test_loopback_alias",
]

log = logging.getLogger(__name__)

#: Hosts this process has already refused with the "not an ssh peer" 502.
#: The 502 body carries the whole remedy on EVERY call; the WARNING line is
#: written once per host so the FORWARDING host's listen journal
#: (``journalctl -u sac-listen`` — the unit's stderr) names the fault
#: without repeating it for every retry the sender makes.
_WARNED_UNROUTABLE_HOSTS: set[str] = set()


def _is_test_loopback_alias(target_host: str) -> bool:
    """True only for the in-process two-listen TEST topology's host labels.

    ``host-a`` / ``host-b`` (any ``host-*``) are the labels
    ``tests/scitex_agent_container/_listen/test_server.py`` stamps on
    ``instances`` rows so two real ``uvicorn`` listens on ONE machine can
    play two hosts; :func:`_forward_via_http` rewrites them to
    ``127.0.0.1``. No fleet host is named this way (fleet names are
    ``scitex-<kind>-NN``), and this predicate is the ONLY gate under which
    the forwarder ever posts plain HTTP — it is the test topology, never
    production. The match is byte-for-byte the rewrite ADR-0015 froze
    ("do not broaden or tighten the ``host-*`` loopback rewrite").
    """
    return target_host.startswith("host-")


@dataclass(frozen=True)
class _PeerRoute:
    """What :func:`_resolve_ssh_peer` decided about one destination host.

    ``ssh_target`` is the alias to dial (``None`` when the host is not an
    ssh peer); ``config_path`` names the config.yaml THIS host resolved
    so the refusal can point at the file to edit; ``reason`` says, in
    operator terms, why no ssh route exists (empty when one does).
    """

    ssh_target: str | None
    config_path: str
    reason: str = ""


def _resolve_ssh_peer(target_host: str) -> _PeerRoute:
    """Resolve ``target_host`` to an ssh alias through the CLI's SSoT.

    Same map ``sac host probe`` / ``sac host exec`` dispatch on:
    config.yaml ``peers:`` (glob keys included — ``PeersMap`` resolves
    them) UNION the scitex-dev host registry rows that carry an
    ``ssh_alias``. A registry row WITHOUT an alias (inbound ssh not
    possible) is deliberately not a route, so it lands in the refusal.

    A config.yaml that fails to parse must not disable forwarding: the
    registry-merged map is still consulted (with an empty config block)
    and the parse failure is carried into any refusal's ``reason`` so the
    operator sees BOTH facts in the 502.
    """
    from .._state._peer_resolve import peers_with_registry
    from .._state.host_config import MovingAliasError, _default_config_path
    from .._state.host_config import load as _load_host_config

    config_note = ""
    try:
        _cfg = _load_host_config()
        raw_peers = _cfg.peers
        config_path = str(_cfg.source_path or _default_config_path())
    except Exception as exc:  # noqa: BLE001  # stx-allow: fallback (reason: a malformed config.yaml must not disable cross-host forwarding — the registry-merged map still routes, and the parse failure is carried into the 502 body and this host's listen journal (journalctl -u sac-listen) via the warning below)
        raw_peers = {}
        config_path = str(_default_config_path())
        config_note = f"{config_path} failed to load ({exc}); "
        log.warning(
            "cross-host forward: %s failed to load (%s); resolving peers "
            "from the scitex-dev host registry alone",
            config_path,
            exc,
        )

    peers = peers_with_registry(raw_peers)
    try:
        spec = peers[target_host]
    except MovingAliasError as exc:
        return _PeerRoute(None, config_path, f"{config_note}{exc}")
    except KeyError:
        spec = None

    if spec is not None and spec.ssh:
        return _PeerRoute(spec.ssh, config_path)
    if spec is not None:
        reason = (
            f"{config_note}{config_path} declares peer {target_host!r} "
            f"but its ssh: target is empty"
        )
    else:
        from scitex_config._ecosystem import local_state as _local_state

        hosts_yaml = _local_state.user_path("dev", "hosts.yaml")
        reason = (
            f"{config_note}it is in neither the peers: block of {config_path} "
            f"nor the scitex-dev host registry ({hosts_yaml}) with an ssh_alias"
        )
    return _PeerRoute(None, config_path, reason)


def _refuse_unroutable(
    *,
    target_host: str,
    target_port: int,
    target_name: str,
    route: _PeerRoute,
) -> Response:
    """The loud 502 for a destination that is not an ssh peer.

    Names the host, states why plain HTTP is not attempted (the fleet's
    agents bind loopback), and names BOTH fixes. Logged at WARNING once
    per host per process (see :data:`_WARNED_UNROUTABLE_HOSTS`).
    """
    message = (
        f"cross-host forward to {target_name!r} on host {target_host!r} "
        f"refused: {target_host!r} is not resolvable to an ssh peer on this "
        f"host — {route.reason}. The fleet's agents bind 127.0.0.1 (bridge "
        f"and sidecar default host), so a direct HTTP forward to "
        f"http://{target_host}:{target_port} cannot work and was not "
        f"attempted. Fix one of: (1) declare {target_host!r} with an "
        f"ssh_alias in the scitex-dev host registry hosts.yaml; "
        f"(2) add `peers: {{{target_host}: {{ssh: <alias>}}}}` to "
        f"{route.config_path} on this host."
    )
    if target_host not in _WARNED_UNROUTABLE_HOSTS:
        _WARNED_UNROUTABLE_HOSTS.add(target_host)
        log.warning("%s", message)
    return JSONResponse({"error": message}, status_code=502)


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

    **Transport selector**: ``target_host`` is resolved through
    :func:`_resolve_ssh_peer` — config.yaml ``peers:`` (glob keys
    included) UNION the scitex-dev host registry, the same SSoT the
    CLI verbs dispatch on. A resolvable host takes the ssh + remote
    curl leg. A ``host-*`` test-loopback alias
    (:func:`_is_test_loopback_alias`) takes the in-process HTTP leg.
    Anything else is refused with a 502 that names the host and both
    fixes — plain HTTP to a fleet host cannot connect (agents bind
    loopback) and is never attempted.
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

    route = _resolve_ssh_peer(target_host)
    if route.ssh_target:
        return await _forward_via_ssh_curl(
            target_host=target_host,
            target_port=target_port,
            target_name=target_name,
            body=body,
            peer_bearer=peer_bearer,
            ssh_target=route.ssh_target,
        )

    if _is_test_loopback_alias(target_host):
        return await _forward_via_http(
            target_host=target_host,
            target_port=target_port,
            target_name=target_name,
            body=body,
            peer_bearer=peer_bearer,
        )

    return _refuse_unroutable(
        target_host=target_host,
        target_port=target_port,
        target_name=target_name,
        route=route,
    )


async def _forward_via_http(
    *,
    target_host: str,
    target_port: int,
    target_name: str,
    body: dict[str, Any],
    peer_bearer: str,
) -> Response:
    """In-process TEST transport: plain HTTP to the loopback listen.

    Reached only when :func:`_forward_to_remote` found no ssh peer AND
    ``target_host`` is a ``host-*`` test-loopback alias
    (:func:`_is_test_loopback_alias`). Production never lands here:
    fleet agents bind ``127.0.0.1``, so a direct HTTP forward to
    another machine cannot connect, and the selector refuses such a
    host with a 502 instead of calling this.
    """
    import httpx as _httpx

    forward_url = (
        f"http://{target_host}:{target_port}/agents/{target_name}/message:send"
    )
    # In the test loopback both hosts live on 127.0.0.1; the canonical
    # host name is a label, not a routable address. Rewrite to
    # 127.0.0.1 for the known-loopback aliases so the test fixtures can
    # drive both legs on one machine.
    if _is_test_loopback_alias(target_host):
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
    except Exception:  # noqa: BLE001  # stx-allow: fallback (reason: a non-JSON destination body is tolerated and returned verbatim as forwarded_body_text in the a2a response the sender receives)
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
    :class:`.auth.BearerAuthMiddleware` admits the request as an
    administrative caller (same shape as the HTTP path). Cross-group
    ACL denials fire at the destination, not here.

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
