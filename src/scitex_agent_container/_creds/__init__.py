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
(blocked-now — they 429 immediately) and, under the default
``POLICY_SPREAD``, ≥ ~90% of their 7d window (near-capped), and
load-balance the remaining healthy tier across the fleet via per-agent
weighted rendezvous hashing. The opt-in ``POLICY_BURN``
(:mod:`._spend_policy`; activation gated on the fleet reconciler)
inverts the 7d rule: prefer the FULLEST weekly bucket and drain it to
zero, because unspent 7d quota is destroyed at the boundary. Quota
state is a preference, never a gate — in-turn 429s still surface from
claude. Every pool pick logs its full ranking inputs
(:mod:`._pick_audit`) so a pick is auditable after the fact.
"""

from __future__ import annotations

from ._pick_audit import (
    CandidateAudit,
    audit_candidates,
    format_pick_audit,
    pick_audit_parts,
)
from ._pick_healthy import (
    AccountHealth,
    NoHealthyAccountError,
    account_5h_usage,
    account_7d_usage,
    account_health,
    pick_healthy_account,
)
from ._quota_rank import account_7d_reset_at
from ._spend_policy import (
    POLICY_BURN,
    POLICY_SPREAD,
    VALID_7D_POLICIES,
    resolve_7d_policy,
)

__all__ = [
    "AccountHealth",
    "CandidateAudit",
    "NoHealthyAccountError",
    "POLICY_BURN",
    "POLICY_SPREAD",
    "VALID_7D_POLICIES",
    "account_5h_usage",
    "account_7d_reset_at",
    "account_7d_usage",
    "account_health",
    "audit_candidates",
    "format_pick_audit",
    "pick_audit_parts",
    "pick_healthy_account",
    "resolve_7d_policy",
]
