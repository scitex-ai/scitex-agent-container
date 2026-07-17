"""7d spend policy for the account picker (operator ruling 2026-07-17).

Split out of :mod:`._quota_rank` (512-line module limit). This module
owns the POLICY layer of the quota-conditional pick — which rule the
ranking applies to the 7-day window — while ``_quota_rank`` keeps the
tiering/spread machinery and ``_pick_healthy`` the freshness gate.

Why two policies (operator, 2026-07-17)
---------------------------------------
The 5h and 7d windows are DIFFERENT KINDS of resource and must not
share a rule:

* **5h window — ROLLING.** Hitting the cap costs a short wait and the
  capacity returns on its own. Avoiding a blocked-now account is
  reasonable; you lose little by waiting.
* **7d window — WEEKLY, USE-IT-OR-LOSE-IT.** Unspent 7d quota is
  DESTROYED at the boundary. It does not roll over. "Avoid the
  near-capped 7d account" therefore PRESERVES nothing — it throws the
  remainder away.

:data:`POLICY_SPREAD` (default) — the historical ranking: demote
7d-near-capped accounts (unless expiring within the
``EXPIRING_7D_HORIZON_S`` window) and spread the fleet by 7d headroom
via weighted rendezvous hashing.

:data:`POLICY_BURN` (opt-in) — the operator's corrected rule: 「7d 上限
が近ければむしろ積極的に使って使い切る；落ちたら再起動」. Among
token-fresh, 5h-unblocked accounts prefer the HIGHEST 7d usage (spend
the perishable weekly bucket to zero), tie-break by SOONEST 7d reset;
when the account blocks, the next boot fails over.

*** ACTIVATION IS GATED — DO NOT FLIP THE DEFAULT. *** Burn-to-zero
deliberately manufactures agent deaths (「落ちたら再起動」), which is
only safe when restart is AUTOMATIC. The fleet reconciler (card
``sac-fleet-reconciler-restart-dead-agents-20260716``) is not live —
``restart.policy`` is dead code in all 93 specs — so the default stays
:data:`POLICY_SPREAD` until it is. Opt in per host via
``SAC_CREDS_7D_POLICY=burn`` once the reconciler restarts quota-dead
agents on its own.

Read-only, like everything in ``_creds``: this module never touches a
token (the pool behind the 2026-07-09 load/401 incident — a probe here
must not mutate).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Mapping

# The two 7d spend policies — see the module docstring.
POLICY_SPREAD = "spread"
POLICY_BURN = "burn"
VALID_7D_POLICIES = (POLICY_SPREAD, POLICY_BURN)

# Env suffix read by resolve_7d_policy() — SAC_CREDS_7D_POLICY /
# SCITEX_AGENT_CONTAINER_CREDS_7D_POLICY (see scitex_agent_container._env).
ENV_7D_POLICY_SUFFIX = "CREDS_7D_POLICY"


def resolve_7d_policy(raw: str | None = None) -> str:
    """Resolve the effective 7d spend policy (arg wins, else env, else spread).

    ``raw=None`` reads ``SAC_CREDS_7D_POLICY`` (either sac prefix) via
    :func:`scitex_agent_container._env.getenv`. Empty/unset resolves to
    :data:`POLICY_SPREAD` — the burn-to-zero default is GATED on the
    fleet reconciler (see the module docstring). Any other value must
    be a member of :data:`VALID_7D_POLICIES` (case-insensitive,
    whitespace-tolerant); an unknown value raises ``ValueError`` — an
    operator who asked for a policy must never silently get another.
    """
    if raw is None:
        from .._env import getenv

        raw = getenv(ENV_7D_POLICY_SUFFIX, "") or ""
    value = raw.strip().lower()
    if not value:
        return POLICY_SPREAD
    if value not in VALID_7D_POLICIES:
        raise ValueError(
            f"invalid 7d spend policy {raw!r} (SAC_{ENV_7D_POLICY_SUFFIX}): "
            f"valid values are {', '.join(VALID_7D_POLICIES)}"
        )
    return value


def validate_7d_policy(policy: str) -> None:
    """Fail loud on a policy string outside :data:`VALID_7D_POLICIES`."""
    if policy not in VALID_7D_POLICIES:
        raise ValueError(
            f"invalid 7d spend policy {policy!r}: valid values are "
            f"{', '.join(VALID_7D_POLICIES)}"
        )


def coerce_epoch(value: object) -> float | None:
    """Return *value* as epoch seconds, or ``None`` if unparseable.

    Tolerant on purpose — the quota cache stores the reset stamp as the
    upstream ISO-8601 string (``reset_at_7d``), but tests and callers
    find raw epoch floats easier to reason about, so both are accepted.
    A naive (tz-less) ISO stamp is read as UTC, matching
    :func:`cli_pkg._account_list_format._coerce_dt`.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    # stx-allow: fallback (reason: a malformed cache timestamp must degrade to
    # "reset unknown" — i.e. the pre-existing behaviour — never crash a boot.)
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def pick_burn(
    names: list[str],
    usage_5h: Mapping[str, float | None],
    usage_7d: Mapping[str, float | None],
    resets: Mapping[str, object],
    *,
    blocked_5h_pct: float,
) -> str:
    """The :data:`POLICY_BURN` ordering (operator ruling 2026-07-17).

    1. Tier out 5h-blocked accounts (cannot serve NOW; still a
       preference, not a gate — an all-blocked fleet boots on the
       least-bad tier) and unknown-7d accounts (known usage beats a
       guess).
    2. Within the best tier, HIGHEST 7d usage first — spend the
       perishable weekly bucket to zero; unspent 7d quota is destroyed
       at the boundary.
    3. Tie-break by SOONEST 7d reset (an unknown reset sorts last),
       then candidate order.

    Deterministic — no spread key. Concentrating agents on the near-cap
    account until it blocks is the POINT (「落ちたら再起動」); fail-over
    happens on the next boot, once the reconciler owns the restart.
    """

    def tier(name: str) -> tuple[bool, bool]:
        h5 = usage_5h.get(name)
        return (
            h5 is not None and h5 >= blocked_5h_pct,
            usage_7d.get(name) is None,
        )

    best = min(tier(n) for n in names)
    group = [n for n in names if tier(n) == best]

    def sort_key(item: tuple[int, str]) -> tuple[float, float, int]:
        idx, name = item
        d7 = usage_7d.get(name)
        reset_at = coerce_epoch(resets.get(name))
        return (
            -(d7 if d7 is not None else -math.inf),
            reset_at if reset_at is not None else math.inf,
            idx,
        )

    return min(enumerate(group), key=sort_key)[1]


__all__ = [
    "ENV_7D_POLICY_SUFFIX",
    "POLICY_BURN",
    "POLICY_SPREAD",
    "VALID_7D_POLICIES",
    "coerce_epoch",
    "pick_burn",
    "resolve_7d_policy",
    "validate_7d_policy",
]
