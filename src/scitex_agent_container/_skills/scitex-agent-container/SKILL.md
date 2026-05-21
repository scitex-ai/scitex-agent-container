---
name: scitex-agent-container
description: |
  [WHAT] Declarative YAML-based AI agent lifecycle management — define an agent in one `spec.yaml` (apptainer image, model, MCP, mounts/env, health, restart, A2A port, remote host) and `sac agents start` brings it up as a long-lived Claude SDK session inside Apptainer, with A2A inbound (`POST /v1/turn`), SSH remote deployment, and JSON status introspection.
  [WHEN] Use when the user asks to "launch a Claude Code agent", "spawn a fleet of coding agents", "manage agent lifecycle", "run an agent on a remote host", "wire MCP servers into an agent", "talk to a running agent over A2A", or mentions `sac agents start`, `scitex-agent-container`, `spec.yaml`, fleet head/worker.
  [HOW] `pip install scitex-agent-container` then `sac agents start <name>` (CLI) or `import scitex_agent_container`; see leaf skills for details.
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
Define an agent in `spec.yaml`, launch it as a long-lived Claude SDK
session inside Apptainer — locally or on a remote host via SSH — and
observe it through a rich non-agentic status surface (`sac agents
list`/`tail`/`health`).

## What the package ships

| Surface | Location |
|---|---|
| Python API | `scitex_agent_container` (`AgentConfig`, `load_config`, `validate_config`, `Registry`, + namespaced submodules `agent.start`/`stop`/`status`/`logs`, `db.*`, `host.*`, `peer.*`) |
| CLI | `scitex-agent-container`, `sac` — see [10_cli.md](10_cli.md) |
| MCP servers | None bundled — agents spawn their own via `to_home/.mcp.json` |
| Runtimes | `runtimes/claude_session.py` (SDK-native, default); apptainer container build helpers |
| Observability | See [13_observability.md](13_observability.md) |
| Config format | v3 `scitex-agent-container/v3` only — v2 is rejected |

## Sub-skills

### Onboarding & interfaces
- [01_installation.md](01_installation.md) — pip install + auth + per-host hook
- [02_quick-start.md](02_quick-start.md) — first agent in 30 seconds
- [03_python-api.md](03_python-api.md) — programmatic surface (`agent.start`, `peer.post_turn`, ...)
- [04_cli-reference.md](04_cli-reference.md) — every `sac` subcommand
- [05_mcp-tools.md](05_mcp-tools.md) — `sac mcp` server, tool inventory, install snippet (F-CS15)
- [06_http-api.md](06_http-api.md) — `POST /v1/turn` wire format (set `spec.a2a.port` to enable)

### Core
- [01_config-v3.md](01_config-v3.md) — v3 config format (current); v2+`metadata.name` rejected
- [02_multiplexer.md](02_multiplexer.md) — vestigial for agents (SDK runtime, no tmux); lead-only tmux wrap
- [03_auto-accept.md](03_auto-accept.md) — Modular prompt handlers
- [04_resource-management.md](04_resource-management.md) — Resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — Resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — Four env-injection ports + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — Native A2A protocol (`sac a2a serve`)
- [07_a2a-protocol-extension-fields.md](07_a2a-protocol-extension-fields.md) — `x-scitex-agent-container.*` AgentCard extension fields + JSON example
- [08_templates.md](08_templates.md) — Six pattern templates + real-world examples
- [15_claude-session.md](15_claude-session.md) — the claude-session SDK runner (inside the apptainer SIF) + `POST /v1/turn` inbound endpoint; `runtime: apptainer` is the operative runtime
- [16_claude-session-migration.md](16_claude-session-migration.md) — historical: the claude-code → SDK migration is complete; `runtime` is apptainer-only now
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
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract

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
# Each agent lives in its own dir; the dir name is the agent name.
mkdir -p ~/.scitex/agent-container/agents/my-agent
$EDITOR ~/.scitex/agent-container/agents/my-agent/spec.yaml   # see 01_config-v3.md
sac agents start my-agent                       # daemon by default; --foreground streams stdio
sac agents list my-agent --json                 # single-agent status view
sac agents tail my-agent                         # render session.jsonl transcript
```
