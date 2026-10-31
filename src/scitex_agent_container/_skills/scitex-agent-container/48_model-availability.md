---
description: |
  [TOPIC] Main-model and goal-judge availability
  [DETAILS] Verify provider access through official inference and usage endpoints, preserve quota windows, fresh-restart running consumers of capped keys with a bounded recovery budget, and check Say Hi through the real Hermes goal evaluator.
tags: [scitex-agent-container-model-availability]
---

# Main-model and goal-judge availability

Use when a key is capped, a model is unavailable, or the goal judge pauses.
First load [47_hermes-profile-resolution.md](47_hermes-profile-resolution.md)
to identify the host, effective profile, image, model and account pools.
Read scitex-dev's `24_host-registry` skill for fleet routing.

## Preflight before replacing a process

`_lifecycle/_hermes_restart_preflight.prepare_hermes_successor` performs a
synthetic inference check, rejects an invalid-credential control, and checks
main and goal-judge admission independently for each eligible key.
Proof caching distinguishes the roles. The judge follows SAC's verified
provider and model, without silently substituting a hardcoded vendor.

`runtimes/_availability.py` owns the wire request. Use the declared provider's
inference endpoint and protocol; the probe sends no conversation history.
HTTP 200 containing failed inference/error is not success. Timeouts and
transport failures remain unknown. If no successor passes, leave the current
process and generated profile intact.

The native Go route is `https://opencode.ai/zen/go/v1`; custom providers use
their explicit spec endpoint. A model catalog, key shape, package version or
successful request by another client does not prove this client's access.

## Official quota evidence

The account sweep uses provider-owned read-only metadata routes:

| Provider | Endpoint | Evidence |
|---|---|---|
| OpenCode Go | `https://opencode.ai/zen/go/v1/usage` | rolling, weekly and monthly windows |
| CommandCode | `https://api.commandcode.ai/alpha/billing/credits` | balances and window limits |

Other metadata readers are maintained in `_account/provider_usage_http.py`,
including Codex's authenticated usage route. Unsupported shapes and absent
measurements stay unknown. Do not invent a 5h/24h/7d/30d/monthly window that the
provider did not report. Preserve its exact reset time; expired windows are
not current cap evidence.

Probe credentials use the same sourced secret pool as launch: authored file
rotations override stale inherited process values. Compare sanitized fingerprints
and full lengths when a provider rejects authentication. A truncated key and an
expired full key are different faults. CommandCode's explicit expired-key 401 is
reported as `http-401-key-expired`; resetting or restarting cannot renew it.

```bash
# Measure declared slots; no restart requested.
sac accounts probe-api-keys --all --json

# Also fresh-restart running consumers of confirmed capped slots.
sac accounts probe-api-keys --all --restart-capped --json
```

`_account/api_key_liveness.py` classifies measured windows;
`_account/quota_restart.py` applies fresh-restart policy. A running consumer
of a capped declared slot is eligible once per cap event, even if its client
already attempted in-process failover. This is a configured-slot policy,
not a claim that an observer can identify an in-memory active key.

The restart uses normal lifecycle preflight and `session_override="fresh"`.
It must choose a credential proving both main and judge access. Restarting
does not replenish allowance: retain caps/cooldowns and refuse when every
declared key is unavailable. Never erase evidence or repeatedly restart on
the same exhausted account. The shared restart budget, event fingerprint
and running-only selection prevent a restart storm. A busy turn may cause
the lifecycle gate to defer a restart.

The account-key-liveness JobSpec schedules this sweep every 15 minutes through
scitex-dev's jobs plugin. A JobSpec alone does not prove it runs on a host;
verify the actual timer/supervisor and latest receipt.

## Say Hi acceptance check

In an isolated probe session using a copy of the agent's actual config and
auth pool, set the goal to `Say Hi`, request `Say Hi` from the main model, and
pass its actual reply to Hermes `GoalManager.evaluate_after_turn`. Success
requires main inference, a real judge API response, a parsed `done` verdict,
and persisted goal status `done`. A restart message alone is insufficient.

Do not replace a running agent's standing goal with the probe goal. After
repair, inspect the active goal and resume only one paused by judge/API errors,
through `/goal resume` via Hermes' slash-command dispatcher.

Offline regressions cover custom-pool rotation in the pinned Hermes source,
separate main/judge acceptance, provider-reported resets, stale/unknown
evidence, one fresh restart per cap event, and error responses inside HTTP 200.
Run these before rollout; repeat Say Hi against the deployed image/profile.
Use [47](47_hermes-profile-resolution.md) for projection and documentation checks.
