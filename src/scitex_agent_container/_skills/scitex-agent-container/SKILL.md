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
- [05_mcp-tools.md](05_mcp-tools.md) — `sac mcp` server + tool inventory
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format (`spec.a2a.port` enables)

### Core
- [01_config-v3.md](01_config-v3.md) — v3 format; dir-as-SSoT
- [02_multiplexer.md](02_multiplexer.md) — vestigial; lead-only tmux wrap
- [03_auto-accept.md](03_auto-accept.md) — modular prompt handlers
- [04_resource-management.md](04_resource-management.md)
- [05_resource-heartbeat.md](05_resource-heartbeat.md)
- [06_env-injection-ports.md](06_env-injection-ports.md) — four env-injection ports
- [07_a2a-protocol.md](07_a2a-protocol.md) — native A2A protocol (`sac a2a serve`)
- [07_a2a-protocol-extension-fields.md](07_a2a-protocol-extension-fields.md) — `x-scitex-agent-container.*` AgentCard fields
- [08_templates.md](08_templates.md) — six pattern templates
- [14_claude-session-state.md](14_claude-session-state.md) — state-dir layout, auth precedence
- [15_claude-session.md](15_claude-session.md) — SDK runner + `POST /v1/turn` inbound
- [16](16_claude-session-migration.md) — historical claude-code→SDK migration
- [17_inbound-turn-endpoint.md](17_inbound-turn-endpoint.md) — `POST /v1/turn` wire format
- [18_full-agent-delegation.md](18_full-agent-delegation.md) — delegate to another *full* agent
- [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md) — stuck-peer recovery + reaper
- [33](33_twin-spawning.md) — context-inheriting twin
- [34](34_spec-is-a-contract-not-state.md) — spec = contract; state = DB

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src files, venv
- [12](12_wsl-connectivity.md) — WSL connectivity notes
- [24_image-build.md](24_image-build.md) — apptainer `.sif` build/rebuild, rebuild-to-ship gotcha
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — `to_home`→`$HOME`, overlay, settings hook
- [26_credentials-rotation.md](26_credentials-rotation.md) — OAuth + auth mechanics
- [26_credentials-rotation-host.md](26_credentials-rotation-host.md) — refresh + one-refresher invariant + host cron
- [26-acct](26_credentials-account-selection.md) — boot account pin/picker + 7d policy
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified re-login + 401 recovery
- [28_credential-refresh.md](28_credential-refresh.md) — refresh creds without restart; agents re-read next turn
- [29_progress-reporting-to-lead.md](29_progress-reporting-to-lead.md) — milestone push via a2a_send
- [30](30_responsiveness-background-work.md) — short turns; long work backgrounded
- [31](31_worktree-path-safety.md) — keep worktrees outside `.claude*/`
- [32](32_nested-apptainer-builds.md)

### Reference
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract
- [42](42_tui-auth-watchdog.md) — TUI auth-banner detection contract (§1–4)
- [43](43_tui-auth-watchdog-maintenance.md) — package matcher, auth-heal guards + extend-matcher runbook (§5–6)
- [44](44_agent-to-agent-recovery.md) — recover a wedged peer: prompt/tmux/MCP/hook decision tree
- [45](45_agent-to-agent-recovery-tmux.md) — the `tmux send-keys -l` recovery recipe + `/mcp` reconnect
- [46](46_agents-list-auth-cache.md) — persisted auth verdict → `auth-failed` in the fleet view
- [47](47_authoritative-vs-convenient-reads.md)

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging
- [40b](40_periodic-drive-consumer.md)
- [41](41_claude-worktree-relocation.md)

## Environment

- [20_env-vars.md](20_env-vars.md) — `SCITEX_*` env vars read at runtime
- [21_cli-startup-budget.md](21_cli-startup-budget.md) — keep `sac --help` < 500 ms via LazyGroup
- [22_host-passthrough.md](22_host-passthrough.md) — `spec.mounts`/`spec.user`/`spec.env` — host fs/git/gh
- [23_telegram-integration.md](23_telegram-integration.md) — Telegram bridge; `telegram_*` MCP tools, lead-only auth

## 30-second start

See [02_quick-start.md](02_quick-start.md) — `pip install
scitex-agent-container`, drop a `spec.yaml` under
`~/.scitex/agent-container/agents/<name>/`, then `sac agents start
<name>` / `list` / `tail`.
