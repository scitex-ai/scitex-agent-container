---
description: |
  [TOPIC] scitex-agent-container — which OAuth account an agent boots on, and how the 7d quota window is spent.
  [DETAILS] The boot-time decision in `_lifecycle/_start_preflight.py`: `claude.account: <store-name>` as a genuine PIN vs `account: ""` + `credentials_files: [...]` handed to the boot picker (`_creds/_pick_healthy.py`). The `SAC_CREDS_7D_POLICY: spread | burn` spend policy (`_creds/_spend_policy.py`), why 5h and 7d are different kinds of resource, and why `burn` is gated on automatic restart. Reallocation today = restart. On-disk credential model + auth mechanics live in [26_credentials-rotation.md](26_credentials-rotation.md).
tags: [scitex-agent-container-credentials-rotation]
---

# SAC account selection at boot + the 7d spend policy

> On-disk credential model, the binds, and preflight:
> [26_credentials-rotation.md](26_credentials-rotation.md).
> Refresh mechanics + host-side ops:
> [26_credentials-rotation-host.md](26_credentials-rotation-host.md).

Which account an agent boots on is decided ONCE, at start
(`_lifecycle/_start_preflight.py`). Two declarative options in the spec
(operator convention 2026-07-21: write the field explicitly even when
unset — `account: ""` reads as "deliberately unpinned", not "forgot"):

- **`claude.account: <store-name>`** — a genuine PIN to one stored
  OAuth account (`sac accounts list` names). Mutually exclusive with
  `claude.provider`. Credentials resolve to the per-account snapshot
  (the post-#262 live bind).
- **`claude.account: ""` + `claude.credentials_files: [...]`** —
  unpinned: the boot picker ranks the listed candidates by token
  freshness and quota (`_creds/_pick_healthy.py`, quota-conditional via
  the cached `quota-cache.json`).

## The 7d spend policy — `SAC_CREDS_7D_POLICY: spread | burn`

The 5h and 7d quota windows are different kinds of resource
(`_creds/_spend_policy.py`, operator ruling 2026-07-17):

- **5h — rolling.** Hitting the cap costs a short wait; capacity
  returns on its own. Avoiding a blocked-now account is cheap.
- **7d — weekly, use-it-or-lose-it.** Unspent 7d quota is DESTROYED at
  the window boundary. "Avoid the near-capped account" preserves
  nothing — it throws the remainder away.

Valid values:

- **`spread`** (default) — demote 7d-near-capped accounts and spread
  the fleet by 7d headroom via weighted rendezvous hashing. Safe, but
  wastes the perishable remainder of a near-reset account.
- **`burn`** (opt-in) — among token-fresh, 5h-unblocked accounts prefer
  the HIGHEST 7d usage (spend the weekly bucket to zero), tie-break by
  SOONEST 7d reset (`pick_burn`).

**`burn` is deliberately gated.** Burn-to-zero manufactures agent
deaths (「落ちたら再起動」) and is only safe when restart is AUTOMATIC.
Until the fleet reconciler restarts quota-dead agents on its own, the
default stays `spread`; opt in per host with `SAC_CREDS_7D_POLICY=burn`
once it does. An unknown value raises — an operator who asked for a
policy must never silently get another.

Reallocation today = restart (the pick happens only at boot). To move a
running agent onto a specific account: set `claude.account`, then
restart it once its in-flight work allows.

## See also

- [26_credentials-rotation.md](26_credentials-rotation.md) — on-disk
  model, binds, preflight.
- [26_credentials-rotation-host.md](26_credentials-rotation-host.md) —
  refresh mechanics + one-refresher invariant + host cron.
- Source files cited: `_lifecycle/_start_preflight.py`,
  `_creds/_pick_healthy.py`, `_creds/_spend_policy.py`.
