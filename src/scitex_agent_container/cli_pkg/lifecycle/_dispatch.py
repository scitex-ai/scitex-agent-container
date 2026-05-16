#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch for ``sac agents start``.

Step 3a (extraction) of the cross-host pipeline (see
``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``). The
routing branch in ``_start.py`` delegates to :func:`try_dispatch`, which
asks the pure resolver in ``_common._resolve_dispatch_peer`` whether a
remote handoff is required and, when so, calls
:func:`_dispatch_remote_start` to perform the drift check, rsync, and
(in step 4) the remote ``sac agents start`` invocation.

Keeping this code in a sibling module — rather than appending to
``_start.py`` — preserves the per-file 512-line cap and isolates the
dispatch logic for review. Extracted as a pure refactor (no behaviour
change) so the body changes (drift check + rsync) land in an isolated
follow-up diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...config import AgentConfig
from ._common import _resolve_dispatch_peer

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec


def _dispatch_remote_start(
    name: str,
    peer: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Dispatch ``sac agents start <name>`` to a remote ``peer``.

    Step 2 lands only the routing branch in ``_start.py``; the actual
    drift check, rsync, ssh invocation, JSON parse, and lead-side
    registry-row write arrive in steps 3-6. Until then this helper
    refuses to run — a deliberate loud failure so the dispatch branch
    can't silently look-successful on a half-built code path.

    Raises:
        NotImplementedError: always, until step 3 lands.
    """
    raise NotImplementedError(
        f"_dispatch_remote_start(name={name!r}, peer={peer!r}, "
        f"dry_run={dry_run}, force={force}) is not yet implemented. "
        f"Step 3 adds the drift check and rsync; see "
        f"~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md "
        f"for the implementation plan."
    )


def try_dispatch(
    config: AgentConfig,
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    dry_run: bool,
    force: bool,
) -> bool:
    """Route ``config`` to a remote peer when its ``spec.host`` demands it.

    Returns ``True`` when the start was dispatched (the caller should
    ``continue`` the per-target loop).  Returns ``False`` when local
    execution should proceed — either because ``spec.host`` is unset,
    equals the current host, or names an unknown host (the resolver
    yields None and the singleton-skip logic downstream decides what
    to do).

    The body change (drift check + rsync + NotImplementedError("step 4"))
    lands in step 3b atop this extraction.
    """
    spec_host = config.hosts_spec.host
    if isinstance(spec_host, list):
        target_host = spec_host[0] if spec_host else None
    else:
        target_host = spec_host or None
    dispatch_peer = _resolve_dispatch_peer(
        target_host=target_host,
        current_host=current_host,
        peers=peers,
    )
    if dispatch_peer is None:
        return False
    _dispatch_remote_start(
        name=config.name,
        peer=dispatch_peer,
        dry_run=dry_run,
        force=force,
    )
    return True


__all__ = ["_dispatch_remote_start", "try_dispatch"]
