"""Dispatch-ledger glue for the outbound peer client (2026-05-22).

Extracted from :mod:`peer` to keep that module under the per-file line
cap (it already grew an extracted ``_peer_timeout`` sibling; this is the
ledger counterpart). These helpers wrap
:mod:`scitex_agent_container._state.dispatch_ledger` so the peer client
can mint + record + transition a dispatch-ledger row around each
outbound ``/v1/turn`` POST.

The ledger is OBSERVABILITY, not the dispatch itself: a state.db hiccup
must never sink a real send. Every helper here therefore catches + logs
loudly (never silent) instead of letting a ledger write raise into the
transport path.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def self_agent_name() -> str | None:
    """Return the sender's own agent name from the sac env, or None.

    Used to stamp ``from_agent`` on dispatch-ledger rows. Best-effort —
    a script driving ``post_turn`` outside an agent container has no
    ``SAC_NAME`` and the ledger row records ``from_agent=None``.
    """
    from .._env import getenv

    return getenv("NAME")


def record_dispatch_safe(**kwargs: Any) -> str | None:
    """Record a dispatch-ledger row, returning the id (or None on failure).

    Catches + logs (never raises) so a state.db hiccup cannot break the
    actual dispatch — a broken ledger stays visible in the logs.
    """
    from .._state.dispatch_ledger import record_dispatch

    try:
        return record_dispatch(**kwargs)
    except Exception as exc:  # stx-allow: fallback (reason: ledger is observability; a DB write failure must not break the actual dispatch — logged loudly, never silent)
        log.warning("dispatch-ledger record failed: %s", exc)
        return None


def update_dispatch_safe(dispatch_id: str | None, status: str) -> None:
    """Update a dispatch-ledger row's status; log (never raise) on failure."""
    if dispatch_id is None:
        return
    from .._state.dispatch_ledger import update_dispatch_status

    try:
        update_dispatch_status(dispatch_id, status)
    except Exception as exc:  # stx-allow: fallback (reason: ledger is observability; a status-update failure must not break the dispatch — logged loudly, never silent)
        log.warning("dispatch-ledger status update failed: %s", exc)


__all__ = ["record_dispatch_safe", "self_agent_name", "update_dispatch_safe"]
