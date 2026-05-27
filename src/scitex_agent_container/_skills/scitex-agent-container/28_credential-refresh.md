---
description: |
  [TOPIC] Refreshing Anthropic credentials for SAC agents — without restart churn
  [DETAILS] When the host `~/.claude/.credentials.json` is refreshed (OAuth token expiry → re-login / rotation), a RUNNING agent re-reads the fresh MOUNTED credential on its NEXT turn — so `sac agents send <name> "continue"` resumes it, NO restart needed. Cold `sac agents start` is only for `defined`/parked (never-run or stopped) agents. Covers how to tell running from parked, that all three resume paths preserve session_id, the Spartan shared-home propagation shortcut, the credential-mount mechanism, and the `${VAR:+SET}` safe-presence-check rule.
tags: [scitex-agent-container-credential-refresh, credentials, auth, refresh]
---

# Refreshing Anthropic credentials for SAC agents

When a SAC agent's host `~/.claude/.credentials.json` is refreshed
(OAuth token expired and you re-ran `claude /login`, or you rotated the
credential), you do **not** need to reboot the agent. A **running**
agent re-reads the fresh **mounted** credential file on its next turn —
so a single `sac agents send <name> "continue"` resumes it from where it
left off. Restart is reserved for cold/parked agents.

Avoiding the restart matters: rebooting churns the SIF, drops the live
A2A sidecar, and re-pays startup cost — pure waste when the only stale
thing is the auth token.

## TL;DR decision

| Agent state | What it has | Command to resume after creds refresh |
|---|---|---|
| **running** | live A2A sidecar; PID alive | `sac agents send <name> "continue from where you left off"` |
| **defined / parked** (never-run or stopped) | no live sidecar | `sac agents start <name>` |

`sac agents restart <name>` errors `not found in registry` on a
non-running agent — use `start` there, not `restart`.

All three (`send "continue"` / `start` / `restart`) **resume the
agent's `session_id`**, so its conversation and work progress are
preserved.

## Why "continue" works without a restart

The `claude_session` runner provisions auth **at boot** from
`~/.claude/.credentials.json` (see
`runtimes/_sdk_common.py::provision_anthropic_auth` and the
delivery mechanics in [25_claude-setup-delivery.md](25_claude-setup-delivery.md)).
That credential file is **bind-mounted** into the container, not copied,
so the host-side file and the in-container view are the same bytes. A
live runner re-reads the mounted file on its **next turn** — driving one
more turn with `sac agents send <name> "continue"` is enough to pick up
the fresh token. A cold `start` re-reads the file at boot, which is why
parked agents just need `start`.

## Telling running from parked — the send-answers test

A **running** agent's A2A sidecar answers `sac agents send`. The reply
content does not matter for this test:

```bash
sac agents send <name> "continue from where you left off"
```

- Any `status: ok` reply — including an **auth error** or
  *"out of extra usage"* message — means **the sidecar is alive** (the
  agent answered the turn) → the agent is **running** → `continue` was
  the right call; just re-send once more after the creds are fresh.
- A transport failure (no sidecar / connection refused) → the agent is
  **parked** → use `sac agents start <name>` instead.

`sac agents status <name>` (or `sac agents list --json`) also
distinguishes the states, but the send-answers test is the most direct
signal that the *running runner* will see the refreshed mount.

## Spartan: refresh once, propagate everywhere

Spartan's home filesystem is **shared across compute nodes**. So
refreshing `~/.claude/.credentials.json` **once** on Spartan propagates
the fresh token to **every** compute-node agent at the same path — no
per-node copy. Then `sac agents send <name> "continue"` each running
agent (or `start` the parked ones).

Push the fresh creds with `scp -p` (preserve mode — the file is `0600`)
from a host that already has them:

```bash
scp -p ~/.claude/.credentials.json spartan:~/.claude/.credentials.json
```

Never `cat`, `echo`, or otherwise print the file contents — `scp` moves
the bytes without exposing them in logs or terminal scrollback.

## Security: checking secret env-var presence

When you need to confirm an auth env var is *set* (e.g. debugging which
auth path the runner will take), check **presence only** with
`${VAR:+SET}` — it expands to the literal `SET` when the var is
non-empty and to nothing otherwise, never the secret value:

```bash
echo "SAC_ANTHROPIC_API_KEY=${SAC_ANTHROPIC_API_KEY:+SET}"
echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:+SET}"
```

Do **not** use `${VAR:-...}` (expands the value when set) or a bare
`$VAR` / `echo "$VAR"` — those leak the credential into logs.

## See also

- [01_installation.md](01_installation.md) — auth precedence (`~/.claude/.credentials.json` OAuth vs `SAC_ANTHROPIC_API_KEY`)
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — how the credential file is mounted into the agent container
- [14_claude-session-state.md](14_claude-session-state.md) — `session_id` resume, runner state layout
- [20_env-vars.md](20_env-vars.md) — `SAC_ANTHROPIC_API_KEY` and the auth precedence table
