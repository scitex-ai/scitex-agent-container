"""Per-agent credential selection helpers.

The package owns the *picker* layer above the existing
:mod:`scitex_agent_container._account` store. Where ``_account``
manages stored snapshots (save / sync-live / freshness), ``_creds``
answers the agent-start question:

    Given ``spec.claude.account`` (or no preference), WHICH stored
    account should this agent actually run on right now?

Phase 1 (this module): pick a non-expired snapshot — see
:func:`._pick_healthy.pick_healthy_account`. Cap-state probing is
not cheaply detectable from disk, so a non-expired snapshot reads
as healthy; 5h/7d cap-induced 429s are still surfaced from claude
in-turn — the picker only avoids *known-stale* auth at boot.
"""

from __future__ import annotations

from ._pick_healthy import (
    AccountHealth,
    NoHealthyAccountError,
    account_health,
    pick_healthy_account,
)

__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_health",
    "pick_healthy_account",
]
