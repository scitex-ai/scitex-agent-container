---
name: claude-session-runtime
description: SDK-native agent runtime — drives claude-agent-sdk directly instead of running the claude CLI inside tmux. No TUI prompts, no auto-accept, no pane-state classifier. Project-local agent + state discovery so test fixtures live in the repo. Foreground mode streams assistant output to your terminal; daemon mode heartbeats and writes a transcript. Use when you want programmatic, debuggable, OAuth-flat-rate Claude execution without the CLI runtime's screen-scraping baggage.
tags: [scitex-agent-container, claude-session, sdk]
---

# `runtime: claude-session`

The SDK-native counterpart to the legacy `claude-code` runtime. Where
`claude-code` spawns the `claude` CLI inside tmux/screen and screen-scrapes
the TUI, `claude-session` drives `claude-agent-sdk` from a Python runner —
no terminal multiplexer, no auto-accept handlers, no permission prompts.

Same lifecycle CLI surface (`sac start`, `sac stop`, `sac show-status`,
`sac show-logs`); flip a single YAML key.

## Why use it

| Concern | `claude-code` | `claude-session` |
|---|---|---|
| Process | `claude` binary in tmux | Python runner (no multiplexer) |
| TUI prompts | auto-accept via `tmux send-keys` | `permission_mode='bypassPermissions'` |
| Hooks | shell out to `sac record-hook-event` | Python async callbacks |
| Resume | `claude --resume <uuid>` | `ClaudeAgentOptions(resume=...)` (auto-loaded from `state_dir/session_id`) |
| Quota | poll `claude usage` daemon | accumulated from per-turn `usage` blocks in the SDK message stream |
| Auth | env / `~/.claude/.credentials.json` | env / `~/.claude/.credentials.json` (same — flat-rate OAuth by default) |
| Human attach | `tmux attach` | `--foreground` / `sac show-logs` |

## Minimal YAML

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels: { pattern: claude-session }

spec:
  runtime: claude-session
  model: claude-haiku-4-5
  workdir: /tmp/my-agent

  startup_commands:
    - command: "Reply with the string 'hello' and nothing else."
```

The first non-empty `startup_commands[*].command` is the SDK mission
(seed prompt). `delay` is ignored — the SDK takes a one-shot prompt,
not a timed sequence.

## Operating modes

### Daemon (default — production fleet shape)

```bash
sac start my-agent          # detach, returns once PID file lands
sac show-status my-agent    # heartbeat + sdk_session block
sac show-logs my-agent      # rendered transcript from session.jsonl
sac stop my-agent
```

### Foreground (interactive — terminal visibility)

```bash
sac start my-agent --foreground
# assistant output streams to stdout; runner exits when the turn completes
```

`--foreground` is single-target only; multi-target / directory targets
are rejected since the runner takes over the terminal.

## State layout

Per-agent state lives at `<scope>/runtime/<name>/`:

| File | Contents |
|---|---|
| `pid` | Runner's PID (atomic write — tmp + rename). |
| `heartbeat.json` | `{ts, pid, state}`. State ∈ `starting / idle / working / stopping`. Refreshed every 10 s (`--tick-seconds`). |
| `session.jsonl` | One JSON object per turn event: `user / assistant / user_echo / result / error`. The transcript. |
| `session_id` | Latest SDK session UUID. Auto-resumed by the next `sac start`. |
| `quota.json` | Accumulated per-turn token totals (input / output / cache_creation / cache_read / turns). |

Scope resolution (highest priority first):

1. **Project-local**: walks up from cwd to a git repo containing
   `.scitex/agent-container/`. State lands in
   `<repo>/.scitex/agent-container/runtime/<name>/`. Same convention
   as the slurm runtime + scitex-hpc reservations.
2. **Home**: `~/.scitex/agent-container/runtime/<name>/` (when no
   project scope is available).
3. Override via `$SCITEX_AGENT_CONTAINER_RUNTIME_DIR` (the runner reads
   this verbatim — useful for ad-hoc tests).

Symmetric: agent definitions follow the same resolution order
(`<scope>/agents/<name>/<name>.yaml` first, then
`~/.scitex/agent-container/agents/`, then env, then fleet dirs).

## Auth (cost-critical)

The SDK's authentication path, by precedence:

1. `ANTHROPIC_API_KEY` already set → used verbatim.
2. `~/.claude/.credentials.json` exists → SDK reads OAuth token
   automatically (Pro/Max plan, **flat-rate**). The default on every
   workstation that has run `claude /login`.
3. `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY_OAUTH` (sk-ant-oat*) →
   bridged into `ANTHROPIC_API_KEY`. Headless contexts (CI, SLURM,
   cron) — still flat-rate.
4. `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY` (sk-ant-api*) →
   last resort. **Pay-per-token**. Never auto-bridged on top of an
   OAuth path; explicit opt-in only.

Set neither and you get a clear `SDKCommonError` rather than a silent
fall-through to API-key billing.

## Status JSON addition

`sac show-status <name> --json` carries an `sdk_session` field for
agents on this runtime:

```json
{
  "runtime": "claude-session",
  "sdk_session": {
    "session_id": "6ef8248f-1ccd-4877-934d-908e15333b52",
    "quota": {
      "input_tokens": 6,
      "output_tokens": 14,
      "cache_creation_input_tokens": 33003,
      "cache_read_input_tokens": 0,
      "turns": 1
    },
    "heartbeat": {"ts": 1777766006.95, "pid": 3476972, "state": "idle"},
    "state_dir": "/path/to/.scitex/agent-container/runtime/<name>"
  }
}
```

`sdk_session` is `null` for non-SDK agents (claude-code etc.) — never
absent — so dashboards can switch on its presence without checking
`runtime`.

## In-repo smoke test

`<repo>/.scitex/agent-container/agents/sdk-test/sdk-test.yaml` is a
checked-in fixture that the project-local discovery picks up
automatically when `sac` is invoked from inside the repo:

```bash
cd ~/proj/scitex-agent-container
sac start sdk-test --foreground
# expected: sdk-runtime-ok
```

The `sdk-runtime-smoke` GitHub workflow runs this exact command on every
push to develop / main (and daily) so an upstream SDK breakage surfaces
immediately rather than during the next manual fleet operation.

## When NOT to use it

- **Human-typed interactive sessions.** The CLI runtime's tmux session
  lets you type `/clear`, `/compact`, paste, etc. The SDK runtime takes
  one prompt at start time; future turns require A2A or a different
  inbound channel.
- **Existing fleet agents under heavy load.** Migrate one at a time,
  watch for a release cycle before flipping the next.

## Related skills

- [03_auto-accept.md](03_auto-accept.md) — only relevant under the CLI
  runtime; the SDK runtime makes auto-accept obsolete.
- [13_observability.md](13_observability.md) — the broader status JSON
  contract; this leaf describes the SDK-specific addition.
- [14_pane-actions.md](14_pane-actions.md) — pane actions are
  CLI-runtime specific. SDK runtime equivalents arrive when the runner
  grows an inbound queue (post-Phase 3).
