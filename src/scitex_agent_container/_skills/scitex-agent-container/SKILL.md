---
name: scitex-agent-container
description: |
  [WHAT] Declarative YAML-based AI agent lifecycle management — define an agent in one YAML manifest (runtime, model, MCP, env, health, restart, remote SSH host) and `sac agent start` brings it up with auto-accept TUI handling, SSH remote deployment, A2A protocol sidecar, and live pane-state inspection.
  [WHEN] Use when the user asks to "launch a Claude Code agent", "spawn a fleet of coding agents", "manage agent lifecycle", "run an agent on a remote host", "auto-accept Claude permission prompts", "wire MCP servers into an agent", or mentions `sac agent start`, `scitex-agent-container`, agent YAML, fleet head/worker.
  [HOW] `pip install scitex-agent-container` then `import scitex_agent_container`; see leaf skills for details.
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

Declarative lifecycle management for AI coding agents (Claude Code).
Define an agent in YAML, launch it in a tmux (default) or screen
session locally or on a remote host via SSH, and observe it through a
rich non-agentic status surface.

## What the package ships

| Surface | Location |
|---|---|
| Python API | `scitex_agent_container` (`AgentConfig`, `load_config`, `agent_start`/`stop`/`restart`/`status`/`logs`, `Registry`) |
| CLI | `scitex-agent-container`, `sac` — see [10_cli.md](10_cli.md) |
| MCP servers | None bundled — agents spawn their own via `dot_claude/.mcp.json` |
| Runtimes | `runtimes/claude_session.py` (SDK-native, default); apptainer container build helpers |
| PaneActions | See [14_pane-actions.md](14_pane-actions.md) |
| Observability | See [13_observability.md](13_observability.md) |
| Config format | v3 `scitex-agent-container/v3` only — v2 is rejected |

## Sub-skills

### Onboarding & interfaces
- [01_installation.md](01_installation.md) — pip install + auth + per-host hook
- [02_quick-start.md](02_quick-start.md) — first agent in 30 seconds
- [03_python-api.md](03_python-api.md) — programmatic surface (`agent_start`, `peer.post_turn`, ...)
- [04_cli-reference.md](04_cli-reference.md) — every `sac` subcommand
- [05_mcp-tools.md](05_mcp-tools.md) — `sac mcp` server, tool inventory, install snippet (F-CS15)
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format (set `spec.a2a.port` to enable)

### Core
- [01_config-v3.md](01_config-v3.md) — v3 config format (current); v2+`metadata.name` rejected
- [02_multiplexer.md](02_multiplexer.md) — tmux vs screen
- [03_auto-accept.md](03_auto-accept.md) — Modular prompt handlers
- [04_resource-management.md](04_resource-management.md) — Resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — Resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — Four env-injection ports + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — Native A2A protocol (`sac a2a serve`)
- [07_a2a-protocol-extension-fields.md](07_a2a-protocol-extension-fields.md) — `x-scitex-agent-container.*` AgentCard extension fields + JSON example
- [08_templates.md](08_templates.md) — Six pattern templates + real-world examples
- [15_claude-session.md](15_claude-session.md) — `runtime: claude-session` SDK-native runtime + `POST /v1/turn` inbound endpoint
- [16_claude-session-migration.md](16_claude-session-migration.md) — Migrating an agent to claude-session
- [17_inbound-turn-endpoint.md](17_inbound-turn-endpoint.md) — `POST /v1/turn` wire format + sidecar replacement
- [18_full-agent-delegation.md](18_full-agent-delegation.md) — Delegate multi-step work to another *full* Claude Code agent (vs Task subagent)
- [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md) — Operational deep-dives for sac peer fleets: stuck-peer recovery, reaper pattern, hard/soft skills, Monitor over polling

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src files, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes
- [24_image-build.md](24_image-build.md) — apptainer `.sif` build/rebuild, `@develop` pin, rebuild-to-ship runner/channel changes (gotcha)
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — how sac passes Claude setup explicitly into apptainer agents: `to_home`→`$HOME` 1:1 mirror, overlay/`--home` delivery, `--settings` hook load, `setting_sources=[]`

### Reference
- [13_observability.md](13_observability.md) — `sac agent status` JSON contract
- [14_pane-actions.md](14_pane-actions.md) — Typed pane-mediated operations

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging

## Environment

- [20_env-vars.md](20_env-vars.md) — `SCITEX_*` env vars read at runtime
- [21_cli-startup-budget.md](21_cli-startup-budget.md) — keep `sac --help` < 500 ms via LazyGroup
- [22_host-passthrough.md](22_host-passthrough.md) — `spec.mounts` + `spec.user` + `spec.env` for SDK agents that need host filesystem / git / gh
- [23_telegram-integration.md](23_telegram-integration.md) — Telegram fold (Phase 2+3): `_telegram/` bridge, six `telegram_*` MCP tools, channel-push inbound, lead-only auth gate, per-bot-token flock singleton

## 30-second start

```bash
pip install scitex-agent-container
sac agent start my-agent.yaml
sac agent status my-agent --json | jq .pane_state
sac agent attach my-agent           # Ctrl-B D to detach
```
