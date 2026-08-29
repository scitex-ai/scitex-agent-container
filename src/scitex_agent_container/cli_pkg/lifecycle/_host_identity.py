#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WHICH MACHINE IS WHICH — the one place a host name is matched to a machine.

Extracted from :mod:`._common` (a grab-bag at its 512-line cap) because
this is one cohesive responsibility, and because a defect measured on
2026-08-14 showed it needed a name of its own: sac decided "is this host
name ME?" by STRING-COMPARING the pin against ``hostname -s`` and a
sac-local alias table. On ``scitex-nas-03`` — whose ``hostname -s`` is the
appliance's factory name ``DXP480TPLUS-994`` — every spec pinned to its own
fleet name was therefore "neither this machine nor a registered peer", and
``sac agents start scitex-hub`` ON THAT MACHINE refused unless a human
remembered ``--no-redispatch``. The name is not wrong and the machine is
not misnamed; the comparison was missing an authority.

Two questions, one module
-------------------------
* :func:`_local_host_names` — *what is this machine called?* Unions every
  authority that can answer, and is the ONLY place that list is assembled.
* :func:`classify_dispatch_host` — *given a pin, is it me, a peer, or
  nothing I know?* Pure; the caller supplies both ``peers`` and the answer
  to the first question.

Authorities, and why the ledger is one of them
----------------------------------------------
The two sac-local authorities (``config/_host.resolve_hostname`` and
``_state/host_config``'s ``host.aliases``) can both express "this machine
is also called X" — ``host.aliases`` documents ``DXP480TPLUS-994: nas-03``
as its example, in as many words. But they are PER-MACHINE files, so the
mapping only exists where someone remembered to write it, and its absence
is silent until an agent will not start. The ecosystem host registry
(``scitex_dev.hosts``, adapted by ``_state/host_registry``) is the record
every host in the fleet already shares, so identity resolves through it
too — see ``host_registry.registry_local_names``.

Adding it here rather than in a new resolver is deliberate: the union
already existed, and a FOURTH place that knows about host names is exactly
what a fleet with this many name spellings does not need.

What did NOT change: an unroutable pin still fails LOUD. A name that
denotes no machine sac can identify and no peer it can reach is still
``unknown``, and the lifecycle dispatchers still refuse on it
(``_host_routing.format_unknown_host_error``). The case fixed is "you
pinned this to a machine that IS this one, under a different name" — not
"you pinned this somewhere unreachable", which must keep failing.
"""

from __future__ import annotations

import socket
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec

__all__ = [
    "_local_host_names",
    "_resolve_dispatch_peer",
    "classify_dispatch_host",
]


def classify_dispatch_host(
    target_host: str | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
) -> tuple[str, str | None]:
    """Classify a concrete ``spec.host`` into local / remote / unknown.

    Pure resolver — never raises, never logs, never reads files (the
    caller supplies ``local_names`` and ``peers``). This is the operator's
    "resolution layer": a concrete canonical hostname is mapped to WHERE
    that host is, so ``host: <this-machine>`` launches locally and
    ``host: <peer>`` dispatches over ssh.

    Returns a ``(kind, peer)`` tuple:

    * ``("local", None)``  — run on the caller. Fires when ``target_host``
      is unset (empty ``host:`` / absent, normalized to ``""`` → ``None``),
      equals ``current_host``, or is any spelling in ``local_names``
      (the canonical name + aliases that denote THIS machine per
      ``host_config`` AND per the fleet host registry). LOCAL is checked
      BEFORE the peer table so a machine that is ALSO registered as a
      peer (e.g. ``ywata-note-win: {ssh: localhost}`` so remote hosts can
      reach it, or a NAS whose own fleet name is a routable peer name) is
      never ssh-dispatched to itself.
    * ``("remote", <peer>)`` — dispatch to that peer over ssh. Fires when
      ``target_host`` is a known peer key distinct from the local machine
      (glob peer entries like ``spartan-*`` match here via ``PeersMap``).
    * ``("unknown", None)`` — ``target_host`` names neither the local
      machine nor a peer. This classifier stays a PURE resolver and never
      raises; the REACTION is the caller's. Since operator directive
      2026-07-10 the lifecycle dispatchers fail LOUD on it
      (``_host_routing.format_unknown_host_error`` — peer list + fixes)
      instead of silently falling through to a local start; either way an
      unknown host is never routed to ssh.
    """
    if target_host is None:
        return ("local", None)
    if target_host == current_host or target_host in local_names:
        return ("local", None)
    if target_host in peers:
        return ("remote", target_host)
    return ("unknown", None)


def _resolve_dispatch_peer(
    target_host: str | None,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
) -> str | None:
    """Return the peer name to dispatch to, or None for local execution.

    Thin wrapper over :func:`classify_dispatch_host` preserving the historic
    "peer-name-or-None" contract used by ``_dispatch.try_dispatch``. Returns a
    peer name only for a ``remote`` classification; both ``local`` and
    ``unknown`` yield ``None`` (the caller decides what an unknown host
    means). With the default empty ``local_names`` the behaviour is
    byte-identical to the pre-hardening resolver — the alias-of-self
    short-circuit only engages when the caller passes the machine's local
    spellings.
    """
    _kind, peer = classify_dispatch_host(
        target_host, current_host, peers, local_names=local_names
    )
    return peer


def _local_host_names(current_host: str | None = None) -> set[str]:
    """Return every host spelling that denotes THIS machine.

    Unions the THREE hostname authorities so ``host: <canonical-or-alias>``
    resolves to a LOCAL launch regardless of which registry the operator
    configured, and — critically — regardless of drift between them:

      * ``host_config`` (F-CS12 ``~/.scitex/agent-container/config.yaml``):
        ``canonical_host()`` plus the ``host.aliases`` entry keyed by this
        machine's short hostname.
      * ``config/_host.resolve_hostname()`` (the value dispatch already
        passes as ``current_host``) and the bare ``socket`` short hostname.
      * the ECOSYSTEM HOST REGISTRY (``scitex_dev.hosts`` via
        :func:`..._state.host_registry.registry_local_names`) — the fleet
        LEDGER, which knows that one machine answers to several names.

    The registry is consulted LAST and is FED the spellings the first two
    produced, because it answers a pivot: the fleet row claiming one of my
    names hands back that row's other names. It is what lets a machine whose
    ``hostname -s`` looks nothing like its fleet name recognise its own pin —
    ``scitex-nas-03`` reports ``DXP480TPLUS-994`` — without a hand-edited
    per-machine alias table.

    Only the alias entry for THIS machine's short hostname is included, so a
    peer machine's alias is never mistaken for local; likewise the registry
    contributes only when a row claims a name this machine already answers
    to. Best-effort — a missing / broken config or registry degrades to the
    short hostname (plus ``current_host`` when supplied); it never raises.
    """
    names: set[str] = set()
    if current_host:
        names.add(current_host)
    short = socket.gethostname().split(".")[0]
    if short:
        names.add(short)
    # config/_host resolver (env override → spec.hostname_aliases → short).
    try:
        from ...config._host import resolve_hostname

        rn = resolve_hostname()
        if rn:
            names.add(rn)
    except Exception:  # stx-allow: fallback (reason: hostname resolution must never block dispatch; short hostname already captured)
        pass
    # host_config F-CS12 registry (env override → host.canonical → aliases).
    try:
        from ..._state.host_config import load as _load_host_config

        cfg = _load_host_config()
        canonical = cfg.canonical_host()
        if canonical:
            names.add(canonical)
        alias = cfg.host.aliases.get(short)
        if alias:
            names.add(alias)
    except Exception:  # stx-allow: fallback (reason: absent/malformed config.yaml must not break the local-vs-remote decision; the two hostname sources above suffice)
        pass
    # Ecosystem host REGISTRY (the fleet ledger). Fed the spellings above so
    # it can pivot from a name this machine already answers to onto the fleet
    # name(s) for the same machine. Contributes nothing when no row claims
    # this machine, so a host absent from the ledger behaves exactly as before.
    try:
        from ..._state.host_registry import registry_local_names

        names |= registry_local_names(names)
    except Exception:  # stx-allow: fallback (reason: no scitex-dev / no hosts.yaml on this box is a legitimate state — the two authorities above stand, and identity must never become a hard dependency on an optional [dev] extra)
        pass
    return {n for n in names if n}


# EOF
