#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adapter over ``scitex_dev.hosts`` — the ecosystem host-registry PORT.

sac is an ADAPTER, not an owner. ``scitex_dev.hosts`` is the single
place that answers, ecosystem-wide: *where is host X, and what is its
``$SCITEX_DIR`` root?* Its own module docstring names sac explicitly as
a consumer that "should call ``resolve()`` / ``list_hosts()`` here
rather than parsing their own host config or hardcoding a host-specific
absolute path". This module is that call.

What sac KEEPS owning (genuinely sac-specific, not in the registry):
ssh ``via:`` ProxyJump chains, ``env_preamble`` (Lmod on HPC), a2a
ports, peer tokens, the ``lead:`` block. Those stay in
``~/.scitex/agent-container/config.yaml`` (:mod:`.host_config`).

What sac now DEFERS to the registry: "where is host X, and where is
its scitex root".

The ``~`` trap — this is the whole point of the module
--------------------------------------------------------
``HostRecord.scitex_root_path`` expands ``~`` **on the process that
calls it**. For a REMOTE host that is the WRONG home directory — it
silently yields the *local* operator's home. The registry documents
this caveat; sac hit exactly this bug. So:

**This module NEVER expands ``~`` for a remote host.** It exposes only
the RAW ``scitex_root`` string and a strict :func:`remote_state_root`
that returns an absolute root or ``None`` — never a locally-expanded
guess.

Why it matters (measured on Spartan 2026-07-14, not inferred)::

    registry says   spartan.scitex_root = /data/gpfs/projects/punim0264/ywatanabe/.scitex
    reality         ~/.scitex -> /data/gpfs/.../paper-scitex-clew/.scitex   (symlink, Jun 11)

Every consumer that blindly followed the remote's ``~/.scitex`` — sac
included — has been writing the fleet's Spartan state *inside an
unrelated paper project's tree*. Resolving through the registry and
passing the answer to the remote **explicitly** (``SCITEX_DIR=<root>``)
is the fix, and it needs no change on the remote: ``local_state``
already reads ``$SCITEX_DIR`` (default ``~/.scitex``).

Degradation (``scitex-dev`` is a ``[dev]`` extra, not a runtime dep)
-------------------------------------------------------------------
1. Prefer the PORT (``scitex_dev.hosts``) when importable.
2. Else read the registry's OWN data file (``$SCITEX_DIR/dev/hosts.yaml``)
   — the same SSOT bytes, just without the library. This is a degraded
   reader of the SSOT, **not** a second source of truth: sac never
   invents a host list.
3. Else return ``None`` and let callers keep their pre-registry
   behaviour (no regression on a box with no registry at all).

Note — the port SEEDS on read (measured 2026-07-14): ``list_hosts()``
CREATES ``$SCITEX_DIR/dev/hosts.yaml`` from scitex-dev's built-in
defaults when the file is absent, and returns those rows. So step 2 only
ever fires when ``scitex_dev`` itself is not importable, and "this box has
no registry" is not a reachable state whenever the port is installed —
a fresh machine still resolves Spartan's declared root. That is why the
"absent" degradation contract is *the host is not IN the registry*, not
*there is no registry*.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HostView",
    "registry_hosts",
    "registry_scitex_root",
    "registry_ssh_alias",
    "remote_state_root",
]


@dataclass(frozen=True)
class HostView:
    """sac's read-only view of one registry row.

    Deliberately mirrors ``scitex_dev.hosts.HostRecord``'s four fields
    and deliberately OMITS ``scitex_root_path`` — the expanding property
    is the footgun this module exists to keep out of sac (see the module
    docstring). ``scitex_root`` is always the RAW registry string and may
    contain a leading ``~`` meaning *that host's* home, not ours.
    """

    name: str
    kind: str
    ssh_alias: str | None
    scitex_root: str


def _hosts_from_port() -> list[HostView] | None:
    """Read the registry through ``scitex_dev.hosts`` (the preferred port)."""
    try:
        from scitex_dev.hosts import list_hosts
    except Exception:  # stx-allow: fallback (reason: scitex-dev is a [dev] extra, not a runtime dep — absence is expected, not an error)
        return None
    try:
        return [
            HostView(
                name=h.name,
                kind=h.kind,
                ssh_alias=h.ssh_alias,
                scitex_root=h.scitex_root,
            )
            for h in list_hosts()
        ]
    except Exception:  # stx-allow: fallback (reason: a malformed/absent hosts.yaml must degrade to the YAML reader, never crash a lifecycle verb)
        return None


def _hosts_from_yaml() -> list[HostView] | None:
    """Degraded reader of the registry's OWN file (same SSOT bytes).

    Used only when ``scitex_dev`` is not importable. Resolves the path
    exactly as the registry does — ``$SCITEX_DIR/dev/hosts.yaml`` via the
    ecosystem local-state cascade — so this never becomes a second source
    of truth.
    """
    try:
        import yaml
        from scitex_config._ecosystem import local_state as _local_state

        path = _local_state.user_path("dev", "hosts.yaml")
        if not path.is_file():
            return None
        raw = yaml.safe_load(path.read_text()) or {}
        entries = raw.get("hosts") or {}
        if not isinstance(entries, dict):
            return None
        out: list[HostView] = []
        for name, spec in entries.items():
            if not isinstance(spec, dict):
                continue
            root = spec.get("scitex_root")
            if not root:
                continue
            alias = spec.get("ssh_alias")
            out.append(
                HostView(
                    name=str(name),
                    kind=str(spec.get("kind") or "workstation"),
                    ssh_alias=str(alias) if alias else None,
                    scitex_root=str(root),
                )
            )
        return out or None
    except Exception:  # stx-allow: fallback (reason: no registry on this box is a legitimate state — callers keep pre-registry behaviour)
        return None


def registry_hosts() -> list[HostView]:
    """All registry rows (port first, SSOT-YAML fallback, else empty)."""
    return _hosts_from_port() or _hosts_from_yaml() or []


def _find(host: str) -> HostView | None:
    """Resolve one row by registry name, else by ssh_alias."""
    rows = registry_hosts()
    for row in rows:
        if row.name == host:
            return row
    for row in rows:
        if row.ssh_alias and row.ssh_alias == host:
            return row
    return None


def registry_scitex_root(host: str) -> str | None:
    """RAW ``scitex_root`` for ``host``, or ``None`` when unregistered.

    **Never expanded.** A leading ``~`` denotes *that host's* home
    directory and is meaningless on this machine — see the module
    docstring. Callers that need a path to hand to a REMOTE process want
    :func:`remote_state_root`, which refuses to guess.
    """
    row = _find(host)
    return row.scitex_root if row else None


def registry_ssh_alias(host: str) -> str | None:
    """Registry-declared ssh alias for ``host`` (``None`` when local/unknown)."""
    row = _find(host)
    return row.ssh_alias if row else None


def remote_state_root(host: str) -> str | None:
    """ABSOLUTE registry root for ``host``, or ``None`` — never a guess.

    Returns ``None`` in exactly two cases, and both mean *"keep the
    caller's existing home-relative behaviour"*:

    * ``host`` is not in the registry — sac has no better answer than it
      had before, so it must not invent one.
    * the registry root is home-relative (``~/.scitex``) — which already
      IS the remote default (``local_state.user_root()`` falls back to
      ``~/.scitex``). Returning ``None`` keeps mba / nas byte-identical
      to today rather than shipping a locally-expanded ``~`` that would
      resolve to the *lead's* home on the remote — the precise bug this
      module exists to prevent.

    A non-``None`` result is safe to hand to the remote verbatim: as an
    rsync destination prefix, or as ``SCITEX_DIR=<root>`` in the remote
    command. Spartan is the case that matters — its registry root is
    absolute, so the answer is authoritative and no ``~`` is ever
    expanded on either side.
    """
    root = registry_scitex_root(host)
    if not root:
        return None
    root = root.strip()
    if root.startswith("~"):
        return None
    if not root.startswith("/"):
        return None
    return root.rstrip("/")


# EOF
