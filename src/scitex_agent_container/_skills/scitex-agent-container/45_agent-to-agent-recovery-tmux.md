---
description: |
  [TOPIC] The `tmux send-keys -l` recovery recipe — inject keystrokes into a wedged agent's live TUI pane (auth banner / stuck prompt / MCP dialog); the companion HOW to mechanism 2 in [44_agent-to-agent-recovery.md](44_agent-to-agent-recovery.md).
  [DETAILS] Hard-won recipe for the containerized Ink/React `claude` TUI: the `-l` (literal) flag is REQUIRED or keystrokes silently do not land; capture-before → `send-keys -l '<text>'` → `send-keys Enter` (a SEPARATE call, NOT `-l`) → capture-after to confirm the pane changed. Plus the `/mcp`-reconnect-via-tmux sequence and its `-32000` failure → full session restart. Target session is `tui-<name>` on the DEFAULT tmux server (source-verified; always confirm the live name with `tmux ls`). Read [44_agent-to-agent-recovery.md](44_agent-to-agent-recovery.md) for WHEN to reach for this vs a prompt / MCP / hook.
tags: [scitex-agent-container-agent-to-agent-recovery-tmux, recovery, tmux, tui, mcp]
---

# Agent-to-agent recovery — the tmux keystroke recipe

Companion to [44_agent-to-agent-recovery.md](44_agent-to-agent-recovery.md)
(mechanism 2). Use this when a peer is **wedged but its TUI pane is alive**
and both a prompt and `agent_send` have failed to move it. It talks
straight to the screen, so it is immune to the in-container read-path
caveat (44 §3).

## Target the right pane

TUI agents run in tmux session **`tui-<name>`** on the **DEFAULT** server
(no `-L`) — source-verified in `runtimes/_tui_turn_bridge_lifecycle.py`
and `_tui_turn_bridge_port.py`. SDK auto-accept panes are `sac-<name>` on
the `-L sac` server — a *different* concern (see
[42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) §6); do not aim the
recovery at `-L sac`. **Always confirm the live name first** — the
convention is fixed but the running set is not:

```bash
tmux ls                                # sessions on the default server
tmux capture-pane -t tui-<name> -p     # exactly what the agent sees now
```

## The recipe — `-l` is REQUIRED

The containerized Ink/React `claude` TUI **ignores non-literal
send-keys** — without `-l` the keystrokes silently do NOT land (the pane
stays byte-identical). Always split the TEXT (literal, `-l`) from the
SUBMIT (a named key, NOT `-l`), and verify by capturing before and after:

```bash
# 1. capture BEFORE — your baseline
tmux capture-pane -t tui-<name> -p > /tmp/before.txt

# 2. send the TEXT literally (-l). No trailing newline here.
tmux send-keys -t tui-<name> -l 'your instruction text'

# 3. SUBMIT — Enter as a NAMED key, a SEPARATE call, NOT -l
tmux send-keys -t tui-<name> Enter

# 4. capture AFTER — confirm the pane actually changed
tmux capture-pane -t tui-<name> -p > /tmp/after.txt
```

If `before.txt` and `after.txt` are identical, the keystrokes did not
land — the usual cause is a forgotten `-l` on step 2, or the wrong
session name. Re-check `tmux ls`, then retry.

## MCP reconnect via tmux

When a peer's MCP server has dropped (its tools error out mid-session),
drive the in-session reconnect from its pane. The `/mcp` command opens a
modal server dialog; navigate it with arrow keys + Enter (exact key count
depends on how many servers are listed, so capture-after each step):

```bash
tmux send-keys -t tui-<name> -l '/mcp'   # open the MCP-server dialog
tmux send-keys -t tui-<name> Enter
# navigate (arrow keys) to the FAILED server, Enter to select,
# then choose Reconnect — verify with capture-pane between steps:
tmux send-keys -t tui-<name> Down
tmux send-keys -t tui-<name> Enter
```

**`-32000` — when in-session reconnect fails.** A telegrammer MCP
reconnect can come back with a JSON-RPC `-32000` error; the in-session
`/mcp` reconnect then cannot recover it. The fix is a **full session
restart**, which re-spawns the MCP fresh:

```bash
sac agents restart <name>    # in-container: agent_restart / the host broker
```

Prefer a plain `restart` over `--fresh` unless the agent is wedged on a
boot prompt whose queued input keeps returning (then `restart --fresh`).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| pane byte-identical after send | missing `-l` on the text, or wrong session | re-check `tmux ls`; resend text with `-l` |
| text appears but nothing runs | Enter sent WITH `-l` (typed, not a keypress) | send `Enter` as a separate call, no `-l` |
| `/mcp` reconnect returns `-32000` | MCP process unrecoverable in-session | `sac agents restart <name>` (re-spawns MCP) |
| `can't find session` / `no server` | TUI session not running / wrong server | agent may be SDK not TUI, or down — check `agent_status` |

## See also

- [44_agent-to-agent-recovery.md](44_agent-to-agent-recovery.md) — WHEN to use this vs prompt / MCP / hook (the decision tree + the read-path caveat)
- [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) / [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md) — `tui-<name>` vs `sac-<name>` topology, the `tmux capture-pane` matcher
- [03_auto-accept.md](03_auto-accept.md) — the `-L sac` startup-prompt pane (do NOT target it for recovery)
- [27_credentials-relogin.md](27_credentials-relogin.md) / [28_credential-refresh.md](28_credential-refresh.md) — auth recovery a restart applies
