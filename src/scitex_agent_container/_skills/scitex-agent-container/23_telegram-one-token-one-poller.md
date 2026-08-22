---
description: |
  [TOPIC] ONE Telegram bot token, ONE consumer — the two checks that observe it, why neither alone is an all-clear, and the ownership ledger that answers "who holds this bot?".
  [DETAILS] Telegram's getUpdates admits exactly one consumer per token GLOBALLY. `sac doctor --pollers` (`runtimes/_cct_poller_singleton`) reads /proc: it sees a LIVE duplicate including an orphaned poller, and is HOST-SCOPED. `sac doctor --collisions` (`runtimes/_cct_token_collision`) reads SPECS + the secrets pool: it sees a duplicate BEFORE anything starts and ACROSS hosts, and sees no process at all. Measured 2026-08-22: one token held on compute-04 and compute-03 while the per-host probe read ok on both. Both are read-only, three-valued (ok/violation/unknown), never gate a start, and never read a token value — only sha256:<12hex> fingerprints. At start each agent records its claim into the per-host `cct_token_owner` PostgreSQL ledger (write-only; nothing decides on it yet).
tags: [scitex-agent-container-telegram-one-token-one-poller]
---

# One token, one poller — two checks, and neither is enough alone

Companion to `23_telegram-integration.md` (how the token resolves) and
`23_telegram-rail-verdict.md` (what happens when it doesn't). This leaf covers
the opposite failure: the token resolves for **two** agents.

## The invariant

Telegram's `getUpdates` admits exactly ONE consumer per bot token, **globally**.
Two consumers means a 409 conflict loop in which the operator's inbound
messages are dropped — silently, from the only side that matters.

Operator, 2026-08-22 (Telegram 13379), approving the work:
「１トークン１ポーラーですか、はい、お願いします。」

## Two checks, opposite blind spots

| | `sac doctor --pollers` | `sac doctor --collisions` |
|---|---|---|
| reads | `/proc` | specs + the secrets pool |
| module | `runtimes/_cct_poller_singleton` | `runtimes/_cct_token_collision` |
| scope | ONE host | fleet-wide (every spec, whatever host it pins) |
| sees | a LIVE duplicate, incl. an ORPHANED poller whose spec no longer asks for the rail | a duplicate BEFORE either process starts, and ACROSS hosts |
| cannot see | anything on another host | any process at all |

**Measured 2026-08-22.** Fingerprint `00ec09b9ad73` was held by `scitex-hub` on
compute-04 and `proj-scitex-hub` on compute-03 at the same time. The per-host
poller probe returned `ok` on BOTH — correctly, by its own scope. Killing the
duplicate process ended the incident and **fixed nothing**: the two SPECS still
resolved to the same slot, so it would have returned on the next start.

Run both. `sac doctor` with no flags runs the drift check, the poller check and
the collision check together.

## Reading `--collisions`

```
sac doctor --collisions [--json]        # exit 1 on violation OR unknown with --strict
```

- **ok** — every spec that claims a bot claims a different one. Zero claimants
  is ok: nothing can conflict with nothing.
- **violation** — a fingerprint is claimed by two or more specs. Both agents
  and both hosts are named, and cross-host pairs are marked as such.
- **unknown** — the pool read was inconclusive, a spec would not load, or the
  spec tree could not be enumerated. **Never an all-clear.** A fleet full of
  `unknown` usually means ONE thing is wrong (no `SAC_SECRETS_ENVRC` where you
  ran it), not ninety.

A violation OUTRANKS an unknown: a duplicate that has been computed is a fact,
and one broken YAML file must not mute it.

Every result prints its POPULATION — specs examined, how many claim a bot, how
many resolve nothing, how many are deliberately tokenless, how many never ask.
`0 collisions across 0 specs` and `0 across 24` are different facts.

## The three populations that hold NOTHING, and never alarm here

| population | what it is | why it cannot collide |
|---|---|---|
| DISABLED | `spec.apptainer.env: CCT_BOT_TOKEN: ""` | an apptainer `--env` flag overrides `--env-file`, so an explicit empty beats any pool-injected token |
| NO-CHANNEL | the spec never requests `server:claude-code-telegrammer` | bot-less by declaration |
| UNRESOLVED | it requests the rail and no slot resolves | it holds no token to duplicate |

**DISABLED is the invariant upheld by hand, not a defect.** Seven of the eight
handymen carry an explicit empty token so that only `handyman-06` polls the
shared handyman bot. A check that flagged that family would be flagging the
answer.

**UNRESOLVED is a real fault and a DIFFERENT one** — mute and deaf, owned by
`sac agents cct-audit` and `runtimes/_cct_rail_alarm`. It is counted and named
in every collision result and deliberately does not alarm there: measured
2026-08-12, 81 specs declared the channel and 15 resolved a token, so alarming
on it here would fire on 66 agents forever.

## Fixing a violation

Decide which agent OWNS the bot. For each of the others, ONE of:

```yaml
spec:
  apptainer:
    env:
      CCT_BOT_TOKEN_SLOT: <ITS-OWN-SLOT>   # (a) give it its own bot
      CCT_BOT_TOKEN: ""                    # (b) tokenless BY DECLARATION
```

or (c) drop `server:claude-code-telegrammer` from `spec.claude.channels`.

Then re-run — it must read `ok`. sac does not choose for you and **does not
refuse the start**: both checks are detectors. Enforcement is a separate change
with a much larger blast radius.

## One derivation, three callers

`runtimes/_cct_token_resolution.resolve_cct_token` is the single answer to
"which bot does this spec take". `ensure_cct_bot_token` is that function plus a
file write; the collision census is that function over every spec; the
ownership ledger records what it returns. A second derivation beside it would
be free to drift, and a drifted census reports collisions that do not exist or
misses ones that do.

## The ownership ledger

At every start each agent records its claim into the per-host PostgreSQL store
`cct_token_owner` (`_state/state_db_token_owner.py`):

```
(token_fp, host, agent)  ->  pid, started_at, source, slot
```

so "who holds this bot?" is a query rather than a 409 from Telegram or a
`/proc` scan on every host. The identity is the TRIPLE on purpose: keying on
`token_fp` alone would let the second claimant overwrite the first and render
every collision as one tidy row.

**Write-only for now.** Nothing reads it to make a decision, no start is gated
on it, and the write never raises — an unreachable PostgreSQL prints a line
saying the agent starts normally. A ledger that can refuse a boot is worse than
a missing ledger.

## Secrets

No token VALUE is read, logged, written or transmitted on any of these paths.
Only `sha256:<12hex>` fingerprints (`_account/_rotation_audit.fingerprint_token`
— the one fingerprint helper; do not write a second), slot NAMES and pool
source PATHS appear in any verdict, table, JSON payload or ledger row.
