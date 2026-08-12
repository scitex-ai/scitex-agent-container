"""Peer resolution: config.yaml peers UNION the scitex-dev host registry.

Why this module exists (measured 2026-08-12 on scitex-compute-04)
-----------------------------------------------------------------
``sac host probe`` / ``sac host exec`` / ``sac --on <peer>`` all gated on
``peer in cfg.peers`` — the ``peers:`` block of
``~/.scitex/agent-container/config.yaml``. That file is OPTIONAL and
:func:`.host_config.load` is explicitly missing-tolerant, so on a host
with no config.yaml ``cfg.peers`` is ``{}`` and **every** peer name was
rejected with ``exit 2``::

    $ sac host list          # prints 6 registry rows with ssh aliases
    peers: []
    $ sac host probe mba
    error: peer 'mba' is not defined in config.yaml

That is the whole reachability surface refusing every host sac had just
finished listing. The consequence was fleet-wide: nothing could answer
"which hosts are reachable?" through the supported CLI, so every caller
hand-rolled ssh instead — which is exactly the bypass a supported surface
exists to prevent.

The registry already had the answer. :mod:`.host_registry` reads
``scitex_dev.hosts`` (SSOT ``$SCITEX_DIR/dev/hosts.yaml``) and exposes
:func:`~.host_registry.registry_ssh_alias` — *"the ~/.ssh/config Host
alias to reach this machine"* — which had **zero** call sites. sac was
resolving the registry for ``scitex_root`` (where a peer writes state)
while ignoring it for ``ssh_alias`` (how to reach that peer at all).

Precedence — config.yaml ALWAYS wins
------------------------------------
A config entry carries things the registry deliberately does not model:
``via:`` ProxyJump chains, ``env_preamble`` (Lmod on HPC), ``reverse_ssh``.
Letting a registry row shadow one of those would silently drop a jump
chain, so the registry only ever FILLS GAPS.

The membership test is ``name in peers``, not ``name in peers.keys()``,
and the difference is load-bearing: :class:`~.host_config.PeersMap`
resolves glob keys in ``__contains__``. With a config entry
``spartan*: {via: [spartan], env_preamble: [...]}``, the name ``spartan``
is ALREADY resolvable by pattern; adding an exact registry row for it
would pre-empt the pattern (exact match wins in ``PeersMap``) and strip
the env_preamble that makes ``apptainer`` reachable there.

Rows with no ``ssh_alias`` (``ywata-note-win`` — inbound ssh to it times
out, so the registry records ``null`` deliberately) are NOT routable and
are skipped: offering a peer whose route is ``None`` would render an ssh
argv with an empty host and fail unreadably.

Not fixed here — a stale route is still a stale route
-----------------------------------------------------
This module hands the registry's declared ``ssh_alias`` to ssh verbatim.
If the registry declares a name ``~/.ssh/config`` does not define, the
dispatch still fails — correctly, and now with the alias visible in the
error rather than hidden behind "not defined in config.yaml". Measured on
this host: ``scitex-nas-01`` / ``scitex-nas-02`` do not resolve, while the
``nas-01`` / ``nas-02`` aliases the ssh config actually defines do. That
is a DATA defect in the registry (scitex-dev owns hosts.yaml), and sac
must not paper over it by guessing at a different name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .host_registry import registry_hosts

if TYPE_CHECKING:
    from .host_config import PeerSpec

__all__ = ["peers_with_registry", "registry_peer_names"]


def peers_with_registry(peers: dict[str, "PeerSpec"]) -> dict[str, "PeerSpec"]:
    """``peers`` plus a routable entry for every registry host it lacks.

    The return value is a :class:`~.host_config.PeersMap`, so glob keys
    from config.yaml keep resolving exactly as before and the result can
    be handed straight to :func:`~._host_ssh.build_ssh_argv` (which does
    ``peers[peer_name]`` and walks ``via:`` through the same mapping).

    Pure and side-effect free; safe to call per dispatch. The registry
    read itself degrades to ``[]`` on a box with no scitex-dev and no
    hosts.yaml, in which case the input mapping is returned unchanged in
    content.
    """
    # Imported here rather than at module scope: ``host_config`` re-exports
    # ``_host_ssh`` at its bottom, and that module imports ``host_registry``
    # — a module-level import of ``host_config`` from here would join that
    # cycle and break whichever of the three is imported first.
    from .host_config import PeersMap, PeerSpec

    merged = PeersMap()
    # Config entries FIRST so their insertion order (which is also the
    # glob-match order in PeersMap) is preserved exactly.
    for name, spec in peers.items():
        merged[name] = spec
    for row in registry_hosts():
        if not row.ssh_alias:
            continue
        if row.name in peers:
            # Exact key OR a config glob pattern that already covers it.
            continue
        merged[row.name] = PeerSpec(name=row.name, ssh=row.ssh_alias)
    return merged


def registry_peer_names(peers: dict[str, "PeerSpec"]) -> set[str]:
    """Names in :func:`peers_with_registry` that came from the registry.

    Lets a caller label its output ("where did this route come from?")
    without re-deriving the precedence rule — the one place that rule is
    written down is :func:`peers_with_registry`.
    """
    return {
        row.name
        for row in registry_hosts()
        if row.ssh_alias and row.name not in peers
    }


# EOF
