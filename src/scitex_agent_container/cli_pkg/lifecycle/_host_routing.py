#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Spec-host route resolution for lifecycle verbs (transparent remote routing).

Operator directive 2026-07-10 (card sac-host-field-transparent-remote-routing):
writing ``host: <peer>`` in an agent spec makes the lifecycle verbs operate on
that host transparently. The rails were already there — this module glues the
two existing dispatchers into one coherent routing story:

* ``start`` routes by ``spec.host`` (``_dispatch.try_dispatch`` — the
  instances row does not exist yet at start time). This module contributes
  the CHAIN-AWARE classification + the fail-loud unknown-host error. The chain
  walk itself lives in :mod:`._host_chain` (one resolver, one reachability
  seam); this module adapts it to the lifecycle verbs' shapes and owns the
  operator-facing messages.
* ``stop`` / ``restart`` route primarily by the agent's active
  ``state.db.instances`` row (``_dispatch.try_dispatch_remote``). This module
  fills the gap when NO row exists on the caller's state.db — e.g. the agent
  was started BY the peer itself, so the caller never recorded a row — by
  falling back to the SPEC's ``host:`` pin (:func:`spec_host_fallback_peer`).
* ``status`` needs no spec-host hop here: dispatched starts/restarts write a
  lead-side ``remote`` instances row (so listings show the peer), the in-SIF
  path auto-proxies ``GET /agents/<name>/status`` to the host listen, and
  ``sac --on <peer> agents ...`` remains the explicit any-verb rail.

Caller-context honesty: the remote hop is OpenSSH (``build_ssh_argv`` —
ProxyJump ``via:`` chains + ``env_preamble``, so a two-tier target like
``spartan-gpgpu*`` rides through the login node). On a BARE HOST that is
always reachable. INSIDE a container it works when the caller's ssh config /
keys are bound in (the fleet's whole-home-bind agents); a capsule without ssh
must broker through the host listen instead (``sac agents restart`` already
falls back to ``SAC_LISTEN_BASE_URL``; start uses ``agent_spawn``).

Kept as a sibling module so ``_stop.py`` / ``_restart.py`` / ``_dispatch.py``
stay under the per-file 512-line cap.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from ._common import _local_host_names
from ._host_chain import (
    UNROUTABLE,
    format_unroutable_chain_error,
    resolve_host_chain,
    ssh_reachability_oracle,
)

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec

    from ._host_chain import HostChainRoute, ReachabilityOracle


class UnknownSpecHostError(RuntimeError):
    """``spec.host`` names neither this machine nor a registered peer.

    Raised instead of the historical SILENT local fall-through so a typo'd
    or unregistered host never quietly launches/acts on the wrong machine
    (operator directive 2026-07-10 — remote placement is first-class, so a
    placement that cannot be routed is an error, not a shrug).
    """


def format_unknown_host_error(
    name: str,
    target_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str = "start",
) -> str:
    """Actionable message for a spec.host that resolves to nowhere.

    Lists the registered peers (the ``sac host list`` view) so the operator
    can immediately see whether the fix is a typo correction or a missing
    ``peers:`` entry in ``~/.scitex/agent-container/config.yaml``.
    """
    peer_names = ", ".join(sorted(peers)) if peers else "(none registered)"
    lines = [
        f"Cannot {verb} agent {name!r}: spec.host {target_host!r} is neither "
        f"this machine nor a registered peer.",
        f"Registered peers: {peer_names}",
        "  (from ~/.scitex/agent-container/config.yaml `peers:`; "
        "inspect with `sac host list`)",
        "Fix ONE of:",
        "  * correct spec.host to this machine's resolved hostname "
        "(`hostname -s`) or a registered peer name,",
        f"  * register {target_host!r} under `peers:` (glob patterns like "
        f"`spartan-*:` with `via: [spartan]` cover login-node-fronted "
        f"compute nodes),",
    ]
    if verb == "start":
        lines.append(
            "  * or pass --no-redispatch to force a LOCAL start despite the pin."
        )
    else:
        lines.append(
            f"  * or run the verb on the owning host explicitly: "
            f"`sac --on <peer> agents {verb} {name}`."
        )
    return "\n".join(lines)


def classify_spec_host_route(
    spec_host: str | list[str] | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
    reachability: "ReachabilityOracle | None" = None,
) -> tuple[str, str | None]:
    """Chain-aware wrapper over :func:`_common.classify_dispatch_host`.

    Thin adapter over :func:`._host_chain.resolve_host_chain` (the one place a
    ``spec.host`` chain is reduced), preserving this function's historic
    ``(kind, peer)`` shape: ``("local", None)`` / ``("remote", peer)`` /
    ``("unknown", None)``. An :data:`._host_chain.UNROUTABLE` route maps onto
    the legacy ``"unknown"`` kind, so existing callers keep failing loud on
    exactly the cases they already did.

    ``spec.host`` may be a single hostname or a FALLBACK CHAIN (list). A plain
    string routes exactly as before and is never probed. A list is walked in
    priority order and the first candidate not positively rejected wins — so a
    chain whose head is unreachable now degrades to the next entry, and a
    chain whose tail names THIS machine still classifies ``local``.

    ``reachability`` is the injected ``(host) -> verdict`` oracle; without one
    every remote candidate is :data:`._host_chain.UNKNOWN` and the head wins,
    which is byte-for-byte the historical reduction.

    Callers that need to SEE the rejected candidates (to render the fail-loud
    message) want :func:`resolve_spec_host_route` instead.
    """
    route = resolve_host_chain(
        spec_host,
        current_host,
        peers,
        local_names=local_names,
        reachability=reachability,
    )
    return route_to_legacy_kind(route)


def route_to_legacy_kind(route: "HostChainRoute") -> tuple[str, str | None]:
    """Collapse a :class:`._host_chain.HostChainRoute` to ``(kind, peer)``.

    ``UNROUTABLE`` becomes ``"unknown"``: to a caller that only asked "can I
    ssh somewhere?", a chain in which every host was rejected and a name that
    resolves nowhere are the same answer — do not dispatch, fail loud.
    """
    if route.kind == UNROUTABLE:
        return ("unknown", None)
    return (route.kind, route.peer)


def resolve_spec_host_route(
    spec_host: str | list[str] | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
    reachability: "ReachabilityOracle | None" = None,
) -> "HostChainRoute":
    """Full chain resolution, candidates and all — see :func:`._host_chain`.

    The richer sibling of :func:`classify_spec_host_route`, for the callers
    that must explain a refusal rather than merely make one.
    """
    return resolve_host_chain(
        spec_host,
        current_host,
        peers,
        local_names=local_names,
        reachability=reachability,
    )


def format_route_error(
    name: str,
    spec_host: str | list[str] | None,
    route: "HostChainRoute",
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str,
    current_host: str = "",
) -> str:
    """Pick the right unroutable message for ``spec_host``'s shape.

    A plain string gets :func:`format_unknown_host_error` VERBATIM — one bad
    name, one explanation, unchanged from before chains existed. A list gets
    :func:`._host_chain.format_unroutable_chain_error`, which must account for
    every entry because the operator cannot otherwise tell which of them is a
    typo and which is merely down.
    """
    if isinstance(spec_host, list):
        return format_unroutable_chain_error(
            name, route, peers, verb=verb, current_host=current_host
        )
    return format_unknown_host_error(name, str(spec_host), peers, verb=verb)


def resolve_start_dispatch_peer(
    name: str,
    spec_host: str | list[str] | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
    reachability: "ReachabilityOracle | None" = None,
) -> str | None:
    """Peer that ``sac agents start`` should dispatch ``name`` to, or None.

    The start-side twin of :func:`resolve_spec_host_peer` — same decision, but
    driven by an ALREADY-LOADED spec (start has the config in hand; stop and
    restart only have a name). Extracted here rather than left inline in
    ``_dispatch.try_dispatch`` so all four "reduce a placement to a route, and
    explain a refusal" behaviours live in one module.

    ``None`` means run locally: an empty ``host:``, a pin that spells this
    machine, or a chain that degraded to this machine. A peer name means ssh.

    A LIST ``spec.host`` gets a real ssh reachability probe (built here when
    the caller passes none, since this function is on the side of the seam
    that already authorizes an ssh hop); a plain STRING is never probed.

    Raises:
        UnknownSpecHostError: nothing in ``spec_host`` is usable — a name that
            resolves nowhere, or a chain whose every candidate was rejected.
            Message body from :func:`format_route_error`; a ``RuntimeError``
            subclass, so callers catching ``RuntimeError`` are unaffected.
    """
    if reachability is None and isinstance(spec_host, list):
        reachability = ssh_reachability_oracle(peers)
    route = resolve_spec_host_route(
        spec_host,
        current_host,
        peers,
        local_names=local_names,
        reachability=reachability,
    )
    if route.kind == UNROUTABLE:
        raise UnknownSpecHostError(
            format_route_error(
                name,
                spec_host,
                route,
                peers,
                verb="start",
                current_host=current_host,
            )
        )
    kind, peer = route_to_legacy_kind(route)
    return peer if kind == "remote" else None


def has_active_row(name: str) -> bool:
    """True iff the caller-side ``instances`` table holds an active row for ``name``.

    Any read failure (state.db missing, schema not created) is "no evidence"
    → False, mirroring ``_common._registry_active_on``'s conservative stance.
    """
    # stx-allow: fallback (reason: a missing/uninitialized state.db is a
    # legitimate no-row answer for the spec-host fallback decision — the
    # row-driven dispatcher already surfaced any real state.db fault)
    try:
        from ..._state.state_db import list_active_instances

        rows = list_active_instances(host=None)
    except Exception:
        return False
    return any(row.get("name") == name for row in rows or ())


def resolve_spec_host_peer(
    name: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str,
    current_host: str | None = None,
    local_names: Collection[str] | None = None,
    reachability: "ReachabilityOracle | None" = None,
) -> str | None:
    """Resolve ``name``'s SPEC ``host:`` pin to a remote peer, or None for local.

    Returns:
        * ``None`` — proceed with the unchanged local path. Fires when the
          spec cannot be resolved/loaded (the local verb owns those error
          messages), when ``spec.host`` is empty, or when it spells THIS
          machine — including when a fallback CHAIN degrades to this machine
          because everything ahead of it was rejected.
        * ``<peer>`` — the first candidate in ``spec.host`` that was not
          positively rejected names a registered peer distinct from this
          machine; the caller dispatches ``verb`` there over ssh.

    ``reachability`` is the injected chain-probe oracle. When ``spec.host`` is
    a LIST and no oracle is passed, one real ssh prober is built here — this
    function already loads config and is about to authorize an ssh hop, so it
    is not a pure resolver and probing is in character. A plain STRING never
    builds or consults an oracle (see :func:`._host_chain.resolve_host_chain`).

    Raises:
        UnknownSpecHostError: nothing in ``spec.host`` is usable — a name that
            resolves nowhere, or a chain whose every candidate was rejected
            (see :func:`format_route_error`).
    """
    # stx-allow: fallback (reason: an unresolvable/unloadable spec must fall
    # through to the local verb's own established error surface — the
    # fallback router only acts on a VALID spec with a routable pin)
    try:
        from ...config import load_config
        from ...config._resolve import resolve_with_prefix

        config = load_config(resolve_with_prefix(name))
    except Exception:
        return None
    if current_host is None:
        try:
            from ...config._host import resolve_hostname

            current_host = resolve_hostname()
        except RuntimeError:  # stx-allow: fallback (reason: hostname resolution failure degrades to alias-set matching only)
            current_host = ""
    if local_names is None:
        local_names = _local_host_names(current_host)
    spec_host = config.hosts_spec.host
    if reachability is None and isinstance(spec_host, list):
        reachability = ssh_reachability_oracle(peers)
    route = resolve_spec_host_route(
        spec_host,
        current_host,
        peers,
        local_names=local_names,
        reachability=reachability,
    )
    if route.kind == UNROUTABLE:
        raise UnknownSpecHostError(
            format_route_error(
                name,
                spec_host,
                route,
                peers,
                verb=verb,
                current_host=current_host or "",
            )
        )
    kind, peer = route_to_legacy_kind(route)
    if kind != "remote" or peer is None:
        return None
    return peer


def spec_host_fallback_peer(
    name: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str,
    reachability: "ReachabilityOracle | None" = None,
) -> str | None:
    """Peer to route ``verb`` to when ``name`` has NO active instances row.

    The row-driven dispatcher (``try_dispatch_remote``) already returned
    False when this is consulted. An existing row — even a LOCAL one — means
    the row's answer stands (a locally-running agent is stopped/restarted
    locally regardless of a drifted spec pin), so the spec fallback only
    engages when no row exists anywhere in the caller's state.db.
    """
    if has_active_row(name):
        return None
    return resolve_spec_host_peer(
        name, peers, verb=verb, reachability=reachability
    )


__all__ = [
    "UnknownSpecHostError",
    "classify_spec_host_route",
    "format_route_error",
    "format_unknown_host_error",
    "has_active_row",
    "resolve_spec_host_peer",
    "resolve_spec_host_route",
    "resolve_start_dispatch_peer",
    "route_to_legacy_kind",
    "spec_host_fallback_peer",
]
