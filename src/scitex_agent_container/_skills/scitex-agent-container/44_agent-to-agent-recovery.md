---
description: |
  [TOPIC] Agent-to-agent recovery — WHEN to use each fleet self-heal mechanism (prompt / tmux client-command / MCP tool / hook) to un-wedge a peer from YOUR session, so agents recover each other into fleet resilience.
  [DETAILS] A wedged agent (auth-expired, MCP disconnected, stuck mid-turn) can be recovered agent-to-agent without the operator. Four mechanisms, one decision tree keyed on the TARGET's state: PROMPT (cct/a2a to a live main loop), CLIENT-COMMAND (`tmux send-keys -l` into a live TUI pane — bypasses registry/MCP; full recipe in the companion [45_agent-to-agent-recovery-tmux.md](45_agent-to-agent-recovery-tmux.md)), MCP (`agent_status` for the verdict, `agent_send` to deliver — but heed the in-container read-path caveat below), and HOOK/cron (the auth-heal watchdog and friends). Load before recovering a peer or before trusting a cross-agent "down" verdict. Grounds in source: `cli_pkg/status_cmds.py` (status brokers to host-listen; health does not), `cli_pkg/_send_resolve.py`, `_state/state_db.py`, `runtimes/_tui_outbound.py`.
tags: [scitex-agent-container-agent-to-agent-recovery, recovery, a2a, tmux, watchdog]
---

# Agent-to-agent recovery (self-heal a wedged peer)

When a fleet agent is wedged — auth-expired, MCP disconnected, stuck
mid-turn — you can recover it **from your own session**, agent-to-agent,
without waiting for the operator. There are **four** mechanisms; each has
a clear WHEN. Pick by the TARGET's state, cheapest first.

## Decision tree — pick by the target's state

| Target state | Use | Why |
|---|---|---|
| **Responsive** — main loop alive, draining its inbox | **Prompt** (cct reply / `a2a_send`) | it reasons + acts agentically; cheapest |
| **Wedged**, but its TUI pane is alive (auth banner, stuck prompt, MCP dialog) | **Client-command** (`tmux send-keys -l`) | bypasses registry / MCP / `agent_send`; keystrokes land on the screen |
| Need a **structured** status or action AND the target resolves | **MCP** (`agent_status` = verdict, `agent_send` = deliver) | programmatic — but heed the read-path caveat in §3 |
| **Recurring / systemic** — should not need a trigger | **Hook / cron** | fires on its own (auth-heal watchdog, format enforcement, drift) |

Escalate down the table only as needed: a responsive agent takes a
prompt; a prompt that queues unread means the loop is wedged → go to the
client-command.

## 1. Prompt — natural language to a LIVE main loop

FIRST choice when the agent is merely **idle**, not wedged. Send a
natural-language instruction to the target's main loop — a cct Telegram
reply, or `a2a_send` (see [07_a2a-protocol.md](07_a2a-protocol.md),
[29_progress-reporting-to-lead.md](29_progress-reporting-to-lead.md)).
The agent reads it on its next turn, reasons, and acts. **Requires a
responsive agent:** if the main loop is not draining its inbox the prompt
just queues unread — that is the signal to escalate to the client-command.

## 2. Client-command — tmux keystrokes into the pane

Use when the agent is **wedged but its TUI pane is alive** — an auth
banner, a stuck prompt, or an MCP dialog you must drive. This works
**regardless of registry / MCP / `agent_send` state**: it talks straight
to the screen, so it is immune to the read-path caveat in §3. **This is
THE reliable last-resort recovery.**

TUI agents run in tmux session **`tui-<name>` on the DEFAULT server**
(SDK auto-accept panes are `sac-<name>` on `-L sac` — a different
concern; see [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) §6).
Always confirm the live session name with `tmux ls` first.

The **`-l` (literal) flag is REQUIRED** for the containerized Ink/React
`claude` TUI — without it, keystrokes silently do not land. The full
recipe (capture-before → send `-l` → submit Enter → capture-after) and
the `/mcp`-reconnect-via-tmux sequence live in the companion
[45_agent-to-agent-recovery-tmux.md](45_agent-to-agent-recovery-tmux.md).
Reach for it whenever a prompt and `agent_send` both fail to move the agent.

## 3. MCP tool — structured, but mind the in-container read-path

`agent_send` / `agent_status` / `a2a_send` are the structured,
programmatic surface — BEST when the target resolves. But there is a
**live gotcha** about WHICH datastore they read.

**Caveat — run FROM INSIDE a container:**

| Command | Reads | Cross-agent verdict |
|---|---|---|
| `agent_status` | brokers to the **host listen** (`_status_via_host_listen`, `status_cmds.py`) → real fleet registry | **trustworthy** |
| `agent_send` · `agents health` · `sac db query --agent` | the **per-agent** `state.db` bound at `/state/<name>` — only that agent's turn-bridge ledgers, NOT the fleet registry | **MIS-REPORTS** other live agents as `stopped` / `not running` / `not found` |

Why: `agents health` builds a local `Registry()` and returns
*"Agent '<name>' not found"* for any peer (no host-listen broker —
`status_cmds.py::health`), and `agent_send` resolves the endpoint from
the local `instances` / port-allocator tables (`_send_resolve.py`,
`_state/state_db.py`). Only `status` brokers to the host.

**RULE:** for a cross-agent verdict from inside a container, trust
**`agent_status`** (or read the host
`~/.scitex/agent-container/runtime/state.db` directly). Do **NOT** trust
an in-container `agent_send` / `agents health` "down" verdict — verify it
before acting on it. (A fix is carded: give `agents health` the same host
broker `status` has, and route `agent_send` via the host listen.)

## 4. Hook / cron — automated, unattended, systemic

For recovery that should fire **without any human or agent trigger**: the
auth-heal watchdog restarts auth-wedged agents (see
[42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) /
[43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md)),
plus format enforcement and drift detection. This is the mechanism a
recurring failure mode graduates to — once a manual recovery repeats,
encode it here so no one has to notice next time.

## Bring a peer UP yourself too — spawn / create

The same "don't wait for the operator" norm extends past un-wedging a
LIVE peer to bringing one **up from stopped, or creating a new one**. If
you are a **developer- or researcher-group** agent and you need a peer
started or created, do it yourself — you are **authorized** (the spawn
ACL allows the dev/researcher groups):

- **Start / spawn** a registered peer: `agent_spawn` (MCP tool) or
  `sac agents start <name>` (CLI). `agent_spawn` brokers to the host
  listen, which runs the `check_spawn` ACL gate and records the lineage
  edge — the sanctioned agent-driven spawn path (ADR-0010 mechanism #3).
- **Create** a not-yet-registered agent from a template first
  (`sac agents create <name> --template …`), then start it. A
  context-inheriting fork of a running agent is `agent_twin`.

Escalate to the operator **only** when the host listen broker itself is
down — a transport timeout / connection-refused on the spawn, which the
enriched `cannot reach listen …` error now flags with the fix
(`sac listen restart`). Anything short of a dead broker is yours to do,
not the operator's to queue.

## Imitate this

Every mechanism above is peer-agnostic: any agent can run it on any other
agent. That symmetry is what makes the fleet self-healing — when you
recover a peer you are also demonstrating the recovery the next agent
will copy.

## See also

- [45_agent-to-agent-recovery-tmux.md](45_agent-to-agent-recovery-tmux.md) — the `tmux send-keys -l` recipe + `/mcp` reconnect (mechanism 2 in depth)
- [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) / [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md) — the auth watchdog (mechanism 4) + `tui-<name>` topology
- [07_a2a-protocol.md](07_a2a-protocol.md) — `a2a_send` bus transport (mechanisms 1/3)
- [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md) — stuck-peer recovery + reaper
- [27_credentials-relogin.md](27_credentials-relogin.md) / [28_credential-refresh.md](28_credential-refresh.md) — auth recovery a restart applies
