#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Spec-host route resolution for lifecycle verbs (transparent remote routing).

Operator directive 2026-07-10 (card sac-host-field-transparent-remote-routing):
writing ``host: <peer>`` in an agent spec makes the lifecycle verbs operate on
that host transparently. The rails were already there — this module glues the
two existing dispatchers into one coherent routing story:

* ``start`` routes by ``spec.host`` (``_dispatch.try_dispatch`` — the
  instances row does not exist yet at start time). This module contributes
  the CHAIN-AWARE classification + the fail-loud unknown-host error.
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

from ._common import _local_host_names, classify_dispatch_host

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec


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
) -> tuple[str, str | None]:
    """Chain-aware wrapper over :func:`_common.classify_dispatch_host`.

    ``spec.host`` may be a single hostname or a FALLBACK CHAIN (list). The
    head of the chain drives the routing decision exactly as before, with
    one refinement: a chain whose head is unknown but whose TAIL contains a
    spelling of THIS machine classifies ``local`` — the documented
    fallback-hosts semantics (``_singleton_skip_reason`` accepts the current
    host anywhere in the chain), which a head-only fail-loud would break.

    Returns the same ``(kind, peer)`` shape as ``classify_dispatch_host``:
    ``("local", None)`` / ``("remote", peer)`` / ``("unknown", None)``.
    """
    if isinstance(spec_host, list):
        chain = [h for h in spec_host if h]
    else:
        chain = [spec_host] if spec_host else []
    head = chain[0] if chain else None
    kind, peer = classify_dispatch_host(
        head, current_host, peers, local_names=local_names
    )
    if kind == "unknown" and any(
        h == current_host or h in local_names for h in chain[1:]
    ):
        return ("local", None)
    return (kind, peer)


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
) -> str | None:
    """Resolve ``name``'s SPEC ``host:`` pin to a remote peer, or None for local.

    Returns:
        * ``None`` — proceed with the unchanged local path. Fires when the
          spec cannot be resolved/loaded (the local verb owns those error
          messages), when ``spec.host`` is empty, or when it spells THIS
          machine.
        * ``<peer>`` — ``spec.host`` names a registered peer distinct from
          this machine; the caller dispatches ``verb`` there over ssh.

    Raises:
        UnknownSpecHostError: ``spec.host`` names neither this machine nor
            a registered peer (see :func:`format_unknown_host_error`).
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
    kind, peer = classify_spec_host_route(
        spec_host, current_host, peers, local_names=local_names
    )
    if kind == "unknown":
        head = spec_host[0] if isinstance(spec_host, list) else spec_host
        raise UnknownSpecHostError(
            format_unknown_host_error(name, str(head), peers, verb=verb)
        )
    if kind != "remote" or peer is None:
        return None
    return peer


def spec_host_fallback_peer(
    name: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str,
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
    return resolve_spec_host_peer(name, peers, verb=verb)


__all__ = [
    "UnknownSpecHostError",
    "classify_spec_host_route",
    "format_unknown_host_error",
    "has_active_row",
    "resolve_spec_host_peer",
    "spec_host_fallback_peer",
]
