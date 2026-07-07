---
description: |
  [TOPIC] Per-agent Telegram bot wake contract — how an idle SDK agent picks up an inbound Telegram message.
  [DETAILS] Each agent runs its OWN claude-code-telegrammer stdio MCP (declared in to_home/.mcp.json under the key `claude-code-telegrammer`). sac's runner injects `CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:<a2a_port>/v1/turn` into that MCP's env when `spec.claude.channels` contains `server:claude-code-telegrammer` AND `spec.a2a.port` is set. The standalone telegrammer poller POSTs each inbound to that URL so an IDLE agent wakes (push ≡ in-session channel). The legacy in-sac `_telegram/` bridge was dropped (`refactor(telegram): drop telegram subsystem from sac`) — this skill documents the current per-agent path + the loud diagnostics that fire when any gate fails.
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
