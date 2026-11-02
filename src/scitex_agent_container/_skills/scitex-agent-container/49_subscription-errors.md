---
description: |
  [TOPIC] Subscription and goal-judge errors
  [DETAILS] Distinguish rejected credentials, missing entitlement, exhausted windows and transport failures; trace main/judge consumers, use verified successor preflight, and recover without leaking keys or restarting indefinitely.
tags: [scitex-agent-container-subscription-errors]
---

# Subscription and goal-judge errors

Use for subscription-required responses, capped API keys, or a goal paused after
consecutive judge errors. First resolve the actual profile with
[47_hermes-profile-resolution.md](47_hermes-profile-resolution.md), then use
[48_model-availability.md](48_model-availability.md) for availability checks.
A banner's example provider/model is not evidence of the live route.

## Classify the actual response

| Evidence | Interpretation and next step |
| --- | --- |
| 401 invalid/expired credential | Check the declared alias and approved secret source. A truncated inherited copy differs from a full expired key. Rotation/re-login must restore a valid credential; restart cannot renew an expired key. |
| 403 with subscription-required text | Treat as credential/product/entitlement rejection, not proof of network outage. Confirm the provider endpoint and account actually belong to the subscribed product. A subscription for one gateway does not grant access through another. |
| Explicit quota/billing response, 402, or current capped usage window | Preserve the provider's reset evidence. Use another eligible declared credential or wait/top up as appropriate. An ordinary transient 429 alone does not establish every quota window. |
| Timeout, DNS/connectivity failure, or plain 5xx without authoritative auth/billing evidence | Availability remains unknown. Check the route inside the actual container; do not declare the key dead or trigger quota recovery from missing evidence. |
| HTTP 200 containing an error or no successful inference | HTTP success is not model access. Require the expected inference result and a discriminating invalid-key control. |

Inspect sanitized status, provider, model, endpoint, account alias and error type.
Read `logs/agent.log` as well as `errors.log`: judge API failures may be logged at
INFO. Never paste complete `.env`, `auth.json`, request headers, pool records, or
user-supplied keys into skills, tickets, screenshots or shell commands.

Keep secrets in the approved private pool. Documentation may name aliases such
as `OPENCODE_GO_API_KEY_PRIMARY` or `COMMANDCODE_API_KEY_PRIMARY`; it must not
contain their values. API keys and OAuth session files are different credential
mechanisms. Do not copy an API key into an OAuth store to silence a warning.

## Trace both consumers

1. Record the live session, effective profile and image/source identity. Compare
   the authored engine/failover policy with the generated main and
   `auxiliary.goal_judge` routes. Do not substitute the example model from a pause
   message or change the authored judge model merely to hide its failure.
2. Check the canonical secret pool, not an inherited shell's stale/truncated
   copies. Compare aliases and sanitized fingerprints/lengths without printing
   values. The account probe reads `read_pool().env` directly.
3. Check native credential-pool identities and auxiliary-client refresh behavior
   in the deployed Hermes version. A named custom provider and legacy
   `custom:<slug>` pool must resolve consistently. The main client can work while
   a cached judge client still holds an older key.
4. Distinguish SAC preflight success from actual Hermes judge success. The
   preflight proves admission for the declared route; it does not prove a stale
   running client's cache was replaced or that a different image contains the
   source fix. Verify the actual judge after recovery.

## Probe and recover within a bounded budget

The examples in [48](48_model-availability.md) are real network operations, not
offline documentation tests. `accounts probe-api-keys` queries supported official
usage/billing endpoints; `--no-record` avoids updating its local verdict cache.
`--restart-capped` additionally invokes lifecycle recovery and may perform small,
potentially billable inference checks. Do not run it as a harmless help command.
Only aliases supported by the usage inventory are covered; this is not a generic
entitlement API for every custom provider.

A normal Hermes successor preflight operates in memory before stopping the old
process. It requires an invalid credential to be rejected and proves main and
judge admission separately. If no declared successor passes, retain the current
process and generated profile. Keep rejected credentials and their reset data;
do not erase cooldowns or loop through restarts to manufacture availability.

Confirmed cap recovery uses `session_override="fresh"` through the normal
lifecycle callback. Restarting is useful to replace stale consumers; it does not
replenish the account. The implemented bounds are:

- Usage evidence must be no more than 300 seconds old. Unknown/stale evidence
  cannot authorize a quota restart.
- Select running consumers of confirmed exhausted declared slots, not every
  stopped agent and not a guessed in-memory active key.
- A successfully handled cap event is fingerprinted and not restarted again.
  Failed/refused attempts still consume the persisted restart budget.
- At most two attempts per sweep, a 1,800-second per-agent debounce, and at most
  two per agent per rolling hour. The callback uses a 30-second drain request;
  lifecycle refusal/busy handling remains authoritative.

Stop when admission cannot be proved or the budget refuses another attempt.
Report aliases, status and reset times; preserve the evidence for the operator.
A scheduled JobSpec is not proof that a host's supervisor/timer is running.

## Verify the repair

Use an owned isolated copy of the actual route/profile for the `Say Hi` check in
[48](48_model-availability.md). Require the real main reply, actual judge API
result, parsed done verdict and persisted goal completion. Never replace an
existing agent's standing goal with the probe goal. After recovering a goal
paused by API errors, resume it through Hermes's native `/goal resume` dispatcher.
A fresh session or restart-success message alone is insufficient evidence.

## Source map

Paths are relative to the installed `scitex_agent_container` package:

- [Canonical pool and CLI sweep](../../cli_pkg/_account_probe_api_keys.py),
  [liveness classification](../../_account/api_key_liveness.py), and
  [official usage readers](../../_account/provider_usage_http.py).
- [Inference request/result semantics](../../runtimes/_availability.py),
  [key status/admission](../../runtimes/_hermes_key_probe.py), and
  [non-mutating successor proof](../../_lifecycle/_hermes_restart_preflight.py).
- [Cap event recovery](../../_account/quota_restart.py),
  [persistent restart limits](../../_reconcile/_budget.py), and
  [scheduled account job declaration](../../_jobs/_specs_accounts.py).

Use the pinned/deployed Hermes implementation for auxiliary-cache and goal
semantics; the host checkout alone does not establish what an existing SIF runs.
