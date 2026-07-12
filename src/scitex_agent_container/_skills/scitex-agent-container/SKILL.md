---
name: scitex-agent-container
description: |
  [WHAT] Declarative YAML AI-agent lifecycle — define an agent in one `spec.yaml`; `sac agents start` runs it as a long-lived Claude SDK session inside Apptainer, with A2A inbound (`POST /v1/turn`), SSH remote deploy, JSON status.
  [WHEN] Launching/managing a Claude Code agent or fleet, running one on a remote host, wiring MCP, talking over A2A, or any mention of `sac agents start`, `scitex-agent-container`, `spec.yaml`, fleet head/worker.
  [HOW] `pip install scitex-agent-container`, then `sac agents start <name>` or `import scitex_agent_container`.
tags: [scitex-agent-container]
primary_interface: cli
interfaces:
  python: 2
  cli: 3
  mcp: 1
  skills: 2
  http: 0
---

# scitex-agent-container

> **Interfaces:** Python ⭐⭐ · CLI ⭐⭐⭐ (primary) · MCP ⭐ · Skills ⭐⭐ · Hook — · HTTP —

Declarative lifecycle management for AI coding agents (Claude Code):
define an agent in `spec.yaml`, launch it as a long-lived Claude SDK
session inside Apptainer (local or remote via SSH), observe via
`sac agents list`/`tail`/`health`.

## What the package ships

| Surface | Location |
|---|---|
| Python API | `scitex_agent_container` (`AgentConfig`, `load_config`, `Registry`; `agent.*`/`peer.*`/`host.*`) |
| CLI | `scitex-agent-container`, `sac` — see [10_cli.md](10_cli.md) |
| MCP servers | None bundled — agents spawn their own via `to_home/.mcp.json` |
| Config format | v3 `scitex-agent-container/v3` only — v2 rejected |

## Sub-skills

### Onboarding & interfaces
- [01_installation.md](01_installation.md) — pip install + auth + per-host hook
- [02_quick-start.md](02_quick-start.md) — first agent in 30 seconds
- [03_python-api.md](03_python-api.md) — programmatic surface (`agent.start`, `peer.post_turn`, ...)
- [04_cli-reference.md](04_cli-reference.md) — every `sac` subcommand
- [05_mcp-tools.md](05_mcp-tools.md) — `sac mcp` server, tool inventory, install snippet
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format (`spec.a2a.port` enables)

### Core
- [01_config-v3.md](01_config-v3.md) — v3 format; dir-as-SSoT; rejects v2-era keys
- [02_multiplexer.md](02_multiplexer.md) — vestigial; lead-only tmux wrap
- [03_auto-accept.md](03_auto-accept.md) — modular prompt handlers
- [04_resource-management.md](04_resource-management.md) — resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — four env-injection ports + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — native A2A protocol (`sac a2a serve`)
- [07_a2a-protocol-extension-fields.md](07_a2a-protocol-extension-fields.md) — `x-scitex-agent-container.*` AgentCard fields
- [08_templates.md](08_templates.md) — six pattern templates + examples
- [14_claude-session-state.md](14_claude-session-state.md) — state-dir layout, auth precedence, `sdk_session` status
- [15_claude-session.md](15_claude-session.md) — SDK runner (in-SIF) + `POST /v1/turn` inbound
- [16_claude-session-migration.md](16_claude-session-migration.md) — historical: claude-code → SDK migration
- [17_inbound-turn-endpoint.md](17_inbound-turn-endpoint.md) — `POST /v1/turn` wire format + sidecar replacement
- [18_full-agent-delegation.md](18_full-agent-delegation.md) — delegate to another *full* agent (vs Task subagent)
- [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md) — stuck-peer recovery + reaper pattern
- [33](33_twin-spawning.md) — context-inheriting twin

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src files, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes
- [24_image-build.md](24_image-build.md) — apptainer `.sif` build/rebuild, rebuild-to-ship gotcha
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — `to_home`→`$HOME`, overlay/`--home`, settings hook
- [26_credentials-rotation.md](26_credentials-rotation.md) — OAuth + auth mechanics
- [26_credentials-rotation-host.md](26_credentials-rotation-host.md) — refresh + one-refresher invariant + host cron rotation
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified re-login (tmux code-paste) + 401-recovery
- [28_credential-refresh.md](28_credential-refresh.md) — refresh creds without restart; running agents re-read on next turn
- [29_progress-reporting-to-lead.md](29_progress-reporting-to-lead.md) — milestone push to lead via a2a_send
- [30_responsiveness-background-work.md](30_responsiveness-background-work.md) — short turns; long work backgrounded so Telegram answers fast
- [31_worktree-path-safety.md](31_worktree-path-safety.md) — keep worktrees outside `.claude*/`; harness reaps that namespace
- [32](32_nested-apptainer-builds.md)

### Reference
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract
- [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) — TUI auth-banner detection contract + extend-the-matcher runbook (the sole safety net for the login-stuck TUI death)

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging
- [40_periodic-drive-consumer.md](40_periodic-drive-consumer.md)
- [41](41_claude-worktree-relocation.md)

## Environment

- [20_env-vars.md](20_env-vars.md) — `SCITEX_*` env vars read at runtime
- [21_cli-startup-budget.md](21_cli-startup-budget.md) — keep `sac --help` < 500 ms via LazyGroup
- [22_host-passthrough.md](22_host-passthrough.md) — `spec.mounts`/`spec.user`/`spec.env` for host fs/git/gh access
- [23_telegram-integration.md](23_telegram-integration.md) — Telegram bridge (`_telegram/`), `telegram_*` MCP tools, channel-push, lead-only auth

## 30-second start

```bash
pip install scitex-agent-container
# Each agent lives in its own dir; the dir name is the agent name.
mkdir -p ~/.scitex/agent-container/agents/my-agent
$EDITOR ~/.scitex/agent-container/agents/my-agent/spec.yaml   # see 01_config-v3.md
sac agents start my-agent          # daemon by default; --foreground streams stdio
sac agents list my-agent --json    # status view
sac agents tail my-agent           # render session.jsonl transcript
```
