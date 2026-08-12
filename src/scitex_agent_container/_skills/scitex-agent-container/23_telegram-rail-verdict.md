---
description: |
  [TOPIC] Is an agent's Telegram rail UP, DOWN or unobserved — and how a MUTE agent raises the alarm when the rail it would use is the broken one.
  [DETAILS] When a spec declares `server:claude-code-telegrammer` and no `CCT_BOT_TOKEN_<SLOT>` resolves, `prune_tokenless_telegrammer_mcp` REMOVES the MCP server (operator ruling). The agent then starts perfectly, reports healthy, and is MUTE and DEAF with no signal anywhere — it cannot even self-diagnose, because `health` is a tool on the server that was removed. `runtimes/_cct_rail_verdict.assess_cct_rail` answers three-valued (up / down / unknown, where `unknown` is never rendered as fine); `runtimes/_cct_rail_alarm` records it in sac's event log under subsystem `cct-rail` and pushes a `blocker` at the LEAD (ADR-0013), i.e. over ANOTHER agent's Telegram. `sac agents cct-audit` sweeps the whole host read-only and exits 1 on any down/unknown. Never gates a start; never reads a token value.
tags: [scitex-agent-container-telegram-rail-verdict]
---

# The Telegram rail verdict — three values, and an alarm not on Telegram

Companion to `23_telegram-integration.md`, which covers the wake contract and
how the bot token is resolved. This leaf covers what happens when it ISN'T.

## The failure

`prune_tokenless_telegrammer_mcp` removes the `claude-code-telegrammer` MCP
entry when no token resolves. That is correct and deliberate — a server that
starts on an empty token and fails on every boot is worse than an absent one.

But it kills the rail in BOTH directions at the one moment nothing can report
it. The agent starts perfectly, reports healthy, and is MUTE (cannot send) and
DEAF (never receives). It cannot even say so: `health` is itself a tool on the
server that just went away. The 2026-08-12 outage was found by the operator
noticing silence — no log, no alert, no failed start.

## Why the mapping breaks: nobody checks the two sides agree

Slot candidates are derived mechanically from the AGENT NAME
(`_cct_token_pool._slot_candidates`). The pool is named by whoever wrote it.
Nothing reconciles them. Measured live on compute-04:

| agent | derives | pool actually has |
|---|---|---|
| `scitex-agent-container` | `SCITEX_AGENT_CONTAINER`, `AGENT_CONTAINER` | `SAC` |
| `scitex-cards` | `SCITEX_CARDS`, `CARDS` | `TODO` (legacy name) |
| `neurovista` | `NEUROVISTA` | `PAPER_NEUROVISTA` |
| `neurovista-paper-writer` | `NEUROVISTA_PAPER_WRITER` | `PAPER_NEUROVISTA_WRITER` |
| `scitex-clew` | `SCITEX_CLEW`, `CLEW` | `PAPER_SCITEX_CLEW` |
| `spartan-dev` | `SPARTAN_DEV` | `DEV` |

The fourth is a WORD-ORDER difference. No stripping rule bridges that, which is
why "derive harder" is not the answer and why sac never guesses: a rule loose
enough to bridge it would also hand some agent a bot that is not its own —
exactly the theft `_slot_candidates` was rewritten in 2026-07 to make
impossible. `near_miss_slots` reports look-alike slots to a HUMAN as a "did you
mean"; sac never resolves through them.

## Three values, and why `unknown` is not a soft `down`

`runtimes/_cct_rail_verdict.assess_cct_rail`:

| verdict | meaning |
|---|---|
| `up` | a token is present — a pool slot resolved, or one was already folded into `$HOME/.env` (precedence #1) |
| `down` | the rail is requested, nothing resolves, AND the pool read was CONCLUSIVE |
| `unknown` | sac could NOT tell |
| `not-requested` | the spec never asked; bot-less by declaration, nothing to be wrong |

`_secret_pool.PoolRead.trusted` is what separates `down` from `unknown`. A read
that sourced no secret FILE holds only the launching process env, which can
prove a slot PRESENT but never proves one ABSENT. A pool that reads clean but
holds no `CCT_BOT_TOKEN_*` at all is likewise `unknown` — that is not a pool
missing this agent's slot, it is not the bot pool.

This is the 2026-08-12 root cause in one flag. The pool file was on the host,
complete; `sac-listen.service` simply had no `SAC_SECRETS_ENVRC`, so three
consecutive diagnoses said "there is no token on 04". The operator's correction
is the specification:

> 「04 にトークンが無い」と私は言ったが誤り。**起動プロセスに無かった**が正しい。
> この区別がバグそのもの。

## The alarm: start and shout, over somebody else's voice

`runtimes/_cct_rail_alarm.check_cct_rail_at_start`, called from
`_lifecycle/_start.py` AFTER `runtime.start` has materialised `$HOME/.env`
(reading it earlier would report a token-less agent that in fact has one).

**It never gates the start.** 81 specs declare the channel and 15 resolve a
token; the request is inherited from the spec templates as scaffolding, so
refusing would refuse most of the fleet. Telegram is a comms rail, not a boot
dependency. And refusing makes the silence worse — a stranded agent cannot do
its non-Telegram work, and the one process that could report the problem is
gone.

Two rails, neither of them the broken agent's Telegram:

* **The record** — `emit_subject_verdicts` under subsystem `cct-rail`
  (DEGRADED / UNKNOWN / HEALTHY, transition-tracked). Durable and sac-owned;
  depends on no lead, no network, no other package.
* **The push** — `push_to_lead(kind="blocker")` (ADR-0013). This reaches the
  operator through the LEAD's Telegram session — a different agent with a
  different bot. **A mute agent shouts with somebody else's voice.**

A log line alone was tried and measured: on 2026-08-10 four agents went mute
behind one INFO line each and the operator concluded they were ignoring him.

The PUSH is gated on evidence somebody meant this agent to have a bot — a
declared slot, a near miss in the pool, or a rail that worked here before —
because paging all 66 `down` agents would rebuild the ignored alert channel the
prune was written to remove. `unknown` always pages: it is rare and usually
systemic (one missing `SAC_SECRETS_ENVRC` blinds the whole fleet). Every
alarming verdict is RECORDED regardless; only the interrupt is rationed.

## The sweep

```
sac agents cct-audit [--json] [--all] [--agents-dir DIR]
```

Read-only: starts nothing, restarts nothing, reads no token value. Lists every
spec that declares the channel with its verdict, the slots tried, and the "did
you mean" column. Exits 1 on any `down`/`unknown`, so a timer or a relocation
preflight can gate on it.

**Run it where agents are STARTED from.** The pool resolves from the launching
environment, so an operator shell, a systemd unit, a non-interactive ssh and a
container each see a DIFFERENT pool. Every run prints which one it read.

## The fix, and secrets

One line in the agent's spec:

```yaml
spec:
  apptainer:
    env:
      CCT_BOT_TOKEN_SLOT: <SLOT>    # precedence #2, WITHOUT the CCT_BOT_TOKEN_ prefix
```

It is preferred over renaming a pool key because it is DECLARED in the spec and
it TRAVELS: a `.envrc`-folded `$HOME/.env` does not survive a relocation, which
is the other half of the 2026-08-12 root cause.

Token values are never read, logged, written or transmitted anywhere on this
path. Presence only; slot NAMES and pool source PATHS at most.
