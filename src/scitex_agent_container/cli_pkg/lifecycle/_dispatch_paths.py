#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Path resolution for cross-host dispatch — near side and far side.

Extracted from :mod:`._dispatch` (512-line cap) because it is one cohesive
responsibility: *given an agent name and a peer, where is the spec HERE and
where must it land THERE?* Both halves were getting this wrong in the same
way — by assuming ``$HOME/.scitex`` is the state root — so they belong
together, stated once.

The near side (:func:`local_spec_dir`)
--------------------------------------
Resolved through the ecosystem local-state cascade (``$SCITEX_DIR``,
default ``~/.scitex``), NOT a bare ``Path.home() / ".scitex"``. Every sac
agent runs with ``$HOME=/home/agent`` inside its container while the
fleet's real root is the operator's home, so a home-anchored lookup reads
an EMPTY shadow tree and reports a spec "not found" for an agent that
plainly exists.

The far side (:func:`remote_spec_dir`)
--------------------------------------
Resolved through the ECOSYSTEM HOST REGISTRY (``scitex_dev.hosts`` — the
SSOT port), never by following the remote's ``~/.scitex``. Measured on
Spartan 2026-07-14::

    registry says   spartan.scitex_root = /data/gpfs/projects/punim0264/ywatanabe/.scitex
    reality         ~/.scitex -> /data/gpfs/.../paper-scitex-clew/.scitex   (symlink)

A HOME-relative destination therefore wrote the fleet's Spartan state inside
an unrelated paper project. ``resolve_peer_scitex_root`` returns an absolute
registry root (inherited through the ``via:`` chain for ``spartan-*`` compute
nodes) or ``None``, in which case the path is left for the PEER's shell to
expand. Nothing home-anchored is ever expanded on THIS machine: for a remote
host it denotes the PEER's home, and expanding it locally yields the lead's.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..._state.host_config import PeerSpec

__all__ = ["local_spec_dir", "remote_spec_dir"]


def local_spec_dir(name: str) -> Path:
    """Where ``name``'s spec lives on the LEAD ($SCITEX_DIR-aware)."""
    from scitex_config._ecosystem import local_state as _local_state

    return _local_state.user_path("agent-container", "agents", name)


def remote_spec_dir(name: str, peer: str, peers: "Mapping[str, PeerSpec]") -> str:
    """Where ``name``'s spec must land on ``peer`` (registry-resolved).

    Returns a PATH, not an rsync target: a registry-pinned absolute root when
    the peer has one, else the literal ``$HOME/.scitex/...``, which the PEER's
    shell expands (:mod:`._spec_handoff` runs it inside ``sh -c``).

    ``$HOME`` rather than the historical bare-relative ``.scitex/...``: a
    relative path resolves against the remote CWD, and that is not always the
    home directory. Measured 2026-08-15 — a non-interactive
    ``ssh scitex-nas-03 pwd`` answers ``/home/ywatanabe/proj/scitex-hub``, so
    the old destination aimed the spec (and, back when the handoff still
    passed ``--delete``, a mirroring delete) at an unrelated repository.

    ``~`` would also have worked here, but only because a shell expands it;
    ``$HOME`` says so explicitly and cannot be mistaken for a path the LEAD
    should expand. Expanding either one locally is always wrong — for a
    remote host they denote the PEER's home, and the lead's is a different
    tree (see the Spartan incident in this module's header).
    """
    from ..._state._host_ssh import resolve_peer_scitex_root

    root = resolve_peer_scitex_root(peer, dict(peers))
    prefix = f"{root}/agent-container" if root else "$HOME/.scitex/agent-container"
    return f"{prefix}/agents/{name}"


# EOF
