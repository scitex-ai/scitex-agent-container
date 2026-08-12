---
description: |
  [TOPIC] Per-agent Telegram bot wake contract — how an idle SDK agent picks up an inbound Telegram message.
  [DETAILS] Each agent runs its OWN claude-code-telegrammer stdio MCP (declared in to_home/.mcp.json under the key `claude-code-telegrammer`). sac's runner injects `CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:<a2a_port>/v1/turn` into that MCP's env when `spec.claude.channels` contains `server:claude-code-telegrammer` AND `spec.a2a.port` is set. The standalone telegrammer poller POSTs each inbound to that URL so an IDLE agent wakes (push ≡ in-session channel). The bot TOKEN is auto-resolved at deploy from the fleet pool (`CCT_BOT_TOKEN_<SLOT>` via `SAC_SECRETS_ENVRC`; `runtimes/_cct_token_pool.py`) into `$HOME/.env` — no per-project `.envrc` required, missing slot = loud ERROR. The legacy in-sac `_telegram/` bridge was dropped (`refactor(telegram): drop telegram subsystem from sac`) — this skill documents the current per-agent path + the loud diagnostics that fire when any gate fails.
tags: [scitex-agent-container-telegram-integration]
---

# Telegram integration — per-agent wake contract

## Design

Each agent runs its OWN Telegram bot via a `claude-code-telegrammer` stdio
MCP declared in the spec's `to_home/.mcp.json`. The in-sac `_telegram/`
bridge from earlier phases was retired (`refactor(telegram): drop telegram
subsystem from sac`). The current path:

1. Operator's `spec.claude.channels` lists `server:claude-code-telegrammer`.
2. Operator's `to_home/.mcp.json` declares a stdio MCP under the key
   `claude-code-telegrammer` (the bun/ts standalone telegrammer process).
3. sac's runner (`runtimes/_sdk_channels.apply_channels`) injects
   `CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:<spec.a2a.port>/v1/turn`
   into that MCP entry's env when (1) AND (2) hold AND `spec.a2a.port` is
   set. Operator-pre-set value wins.
4. Claude Code spawns the telegrammer MCP as a stdio subprocess.
5. The telegrammer JS poller (`ts/lib/wake.ts` in the standalone repo) reads
   `CLAUDE_CODE_TELEGRAMMER_TURN_URL` and on each allowed inbound POSTs the
   message to that URL.
6. The runner's `/v1/turn` handler (`_runners/_session_http.py::serve_inbound`)
   queues a `TurnEnvelope` onto an `asyncio.Queue`. The conversation task
   drains it, drives a turn through the persistent SDK client, replies.

`<channel>` rendering in the AGENT's session requires the dev-channels
flag, which `apply_channels` sets to the comma-joined channel set whenever
ANY `spec.claude.channels` entry is present — the foreign-channel
generalisation guarded by `test__sdk_channels.py`.

## Required spec shape

```yaml
spec:
  a2a:
    port: auto          # MUST NOT be null/missing; the /v1/turn endpoint
                        # is the wake URL the telegrammer POSTs to.
  claude:
    channels:
      - server:claude-code-telegrammer  # exact string (whitespace tolerated)
  # to_home/.mcp.json (sibling file) must contain:
  #   "mcpServers": {"claude-code-telegrammer": {...stdio entry...}}
```

The MCP entry key MUST be `claude-code-telegrammer` (literal). Any other
key — `telegrammer`, `claude-telegrammer`, `tg-bot`, etc. — bypasses the
env injection and the JS poller has no wake URL.

## Bot-token pool — deterministic injection (card sac-fleet-ux-misc-2026-06-24)

Each project runs its OWN bot (unique token per project ⇒ no Telegram 409
single-poller combat). The canonical pool is the set of
`CCT_BOT_TOKEN_<SLOT>` env vars visible to `sac agents start` — its own
environment plus the secret files listed in `SAC_SECRETS_ENVRC`
(colon-separated absolute paths; the same preamble `_envrc` sources, so
daemon-restarted agents resolve identically). On the fleet host that is
the operator's `~/.bash.d/secrets/010_scitex/01_claude-code-telegrammer.src`,
wired into `sac-listen.service` via `Environment=SAC_SECRETS_ENVRC=…`.

At deploy (`runtimes/_cct_token_pool.ensure_cct_bot_token`, called from
`deploy_to_home` AFTER the `.envrc` cascade fold) sac resolves the token
deterministically — per-agent identity never depends on `.envrc` goodwill
(SCITEX_TODO_AGENT incident doctrine). Resolution order, first hit wins:

1. A non-empty `CCT_BOT_TOKEN` already folded into `$HOME/.env` (the
   hand-authored per-project `.envrc` mapping stays authoritative).
2. `spec.apptainer.env: CCT_BOT_TOKEN_SLOT: <SLOT>` — explicit override
   for names that don't map mechanically (e.g. `SAC`). Only that slot is
   tried; a typo fails loud instead of binding another project's bot.
3. Mechanical candidates from the AGENT NAME only: upper-snake, plus the
   same with a leading `scitex-` stripped (`scitex-todo` → `TODO`). The
   WORKDIR is NOT consulted — it was until 2026-07-17, which let a second
   agent in a repo take the first one's bot.

The token lands in `$HOME/.env` (`chmod 0600`, apptainer `--env-file`) —
never on `--env` argv (visible in `/proc/<pid>/cmdline`) and never in a
materialized file (`.mcp.json` keeps `${CCT_BOT_TOKEN}` /
`${CCT_AGENT_ID}` literal for runtime expansion). `CCT_AGENT_ID` defaults
to the workdir basename when no layer set it. Log lines carry only slot
NAMES, paths, and the agent name — token VALUES are never logged.

Channel requested but no slot resolves ⇒ a scitex-logging WARNING names the
pool source, every tried slot and the fixes; the start proceeds (Telegram is a
comms rail, not a boot dependency) but the absence is loud. What happens NEXT —
the three-valued rail verdict, the alarm that reaches the operator over the
LEAD's Telegram rather than the broken agent's, and `sac agents cct-audit` —
is its own leaf: **23_telegram-rail-verdict.md**.

Fix: one line under `spec.apptainer.env` — `CCT_BOT_TOKEN_SLOT: <SLOT>`
(precedence #2, the only route that survives a relocation).

## Failure surface + diagnostics (bug #41 hardening, 2026-06-07)

Each silent-skip in `_wire_telegrammer_wake` used to look identical at the
operator level: "I message my bot, the agent doesn't reply." The wake
helper now LOGs every skip path; the host-side preflight
`validate_telegrammer_wake_wiring` (called from `_lifecycle/_start.py`)
HARD-FAILS the start when the wiring provably won't succeed.

| Failure | Where it surfaces | Operator fix |
|---|---|---|
| `server:claude-code-telegrammer` absent from channels | Silent no-op (intentional — channel not requested) | Add the channel to spec.claude.channels |
| `spec.a2a.port` is null | `validate_telegrammer_wake_wiring` raises `TelegrammerWakeWiringError` at `sac agents start` time | Set spec.a2a.port to 'auto' or an explicit free int |
| `to_home/.mcp.json` missing the `claude-code-telegrammer` MCP entry | Runner-side ERROR log: "no MCP entry keyed 'claude-code-telegrammer' found" | Add the MCP entry under the canonical key |
| `to_home/.mcp.json` entry malformed (`env` not a dict) | Runner-side WARN log | Fix the entry's `env` to be an object |
| Operator pre-set `CLAUDE_CODE_TELEGRAMMER_TURN_URL` | Runner-side INFO log: "pre-set by operator … not overridden" | Verify the pre-set URL actually points at THIS agent's /v1/turn |
| Wake URL successfully wired | Runner-side INFO log: "telegrammer wake wired" | Nothing — verify by tailing runner stderr |
| No `CCT_BOT_TOKEN_<SLOT>` resolves | MCP entry REMOVED (mute + deaf); `cct-rail` `subject-degraded` + lead `blocker`; `cct-audit` DOWN | `CCT_BOT_TOKEN_SLOT: <SLOT>` under spec.apptainer.env, or drop the channel |
| Same, but sac could not READ the pool | `cct-rail` `subject-unknown`; `cct-audit` UNKNOWN, never DOWN | Fix the vantage point (`SAC_SECRETS_ENVRC` on the LAUNCHING process) |

## How to verify on a running agent

```bash
# 1. Confirm the channel + port are configured.
sac agents inspect <name> --json | jq '.spec.claude.channels, .spec.a2a.port'

# 2. Confirm the MCP entry key.
jq '.mcpServers["claude-code-telegrammer"]' \
  ~/.scitex/agent-container/agents/<name>/to_home/.mcp.json

# 3. Tail the runner stderr for the wake-wiring log.
sac agents logs <name> --stderr | grep -i 'telegrammer wake'

# 4. End-to-end: message the bot when the agent is idle, watch for a reply.
```

## Out of scope (not in this repo)

The JS-side telegrammer (`ts/lib/wake.ts`) is in the standalone
[claude-code-telegrammer](https://github.com/ywatanabe1989/claude-code-telegrammer)
repo. If the env var is set but the agent still doesn't wake, the JS
poller may not be reading + POSTing — file that upstream. The Python-side
INFO log "telegrammer wake wired" confirms sac did its half; an idle
agent that still doesn't wake after that points at the JS side.
