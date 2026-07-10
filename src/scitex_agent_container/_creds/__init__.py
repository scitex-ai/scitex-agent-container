"""Per-agent credential selection helpers.

The package owns the *picker* layer above the existing
:mod:`scitex_agent_container._account` store. Where ``_account``
manages stored snapshots (save / sync-live / freshness), ``_creds``
answers the agent-start question:

    Given ``spec.claude.account`` (or no preference), WHICH stored
    account should this agent actually run on right now?

Freshness (a non-expired snapshot) is the hard gate — see
:func:`._pick_healthy.pick_healthy_account`. On top of it the pick is
quota-CONDITIONAL, read cache-only from the bound ``quota-cache.json``
(:mod:`._quota_rank`): avoid accounts at ≥ ~95% of their 5h window
(blocked-now — they 429 immediately) and ≥ ~90% of their 7d window
(near-capped), and load-balance the remaining healthy tier across the
fleet via per-agent weighted rendezvous hashing. Quota state is a
preference, never a gate — in-turn 429s still surface from claude.
"""

from __future__ import annotations

from ._pick_healthy import (
    AccountHealth,
    NoHealthyAccountError,
    account_5h_usage,
    account_7d_usage,
    account_health,
    pick_healthy_account,
)

__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_5h_usage",
    "account_7d_usage",
    "account_health",
    "pick_healthy_account",
]
