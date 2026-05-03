---
name: scitex-agent-container
description: Declarative YAML-based AI agent lifecycle management — define an agent in one YAML manifest (runtime, model, MCP, env, health, restart, remote SSH host) and `sac start` brings it up in tmux/screen with auto-accept TUI handling, SSH remote deployment, sbatch-based SLURM submission with walltime auto-resubmit, A2A protocol sidecar, and live pane-state inspection. Six minimal pattern templates (local / docker / apptainer / ssh / ssh-slurm / mcp) under `config/templates/`. Drop-in replacement for hand-rolled tmux launcher scripts, per-agent shell wrappers, ad-hoc SSH-deploy scripts, manual sbatch wrappers, and bespoke watchdog / auto-accept loops. Use when the user asks to "launch a Claude Code agent", "spawn a fleet of coding agents", "manage agent lifecycle", "run an agent on a remote host / HPC", "submit an agent as a SLURM job", "auto-accept Claude permission prompts", "wire MCP servers into an agent", or mentions `sac start`, `scitex-agent-container`, agent YAML, fleet head/worker, head-mba / head-spartan / head-nas.
primary_interface: cli
interfaces:
  python: 2
  cli: 3
  mcp: 1
  skills: 2
  hook: 0
  http: 0
tags: [scitex-agent-container, scitex-package]
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
| MCP servers | None bundled — agents spawn their own via `src_mcp.json` |
| Runtimes | `runtimes/{tmux,screen,claude_code,apptainer,docker,sbatch_spartan,ssh_remote}.py` |
| PaneActions | See [14_pane-actions.md](14_pane-actions.md) |
| Observability | See [13_observability.md](13_observability.md) |
| Config format | v3 `scitex-agent-container/v3` (current); v2 still supported |

## Sub-skills

### Core
- [01_config-v3.md](01_config-v3.md) — v3 config format (current); v2+`metadata.name` rejected
- [02_multiplexer.md](02_multiplexer.md) — tmux vs screen
- [03_auto-accept.md](03_auto-accept.md) — Modular prompt handlers
- [04_resource-management.md](04_resource-management.md) — Resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — Resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — Four env-injection ports + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — Native A2A protocol (`sac a2a serve`)
- [08_templates.md](08_templates.md) — Six pattern templates + real-world examples
- [09_slurm-tenant.md](09_slurm-tenant.md) — `runtime: slurm-tenant` for shared HPC allocations
- [15_claude-session.md](15_claude-session.md) — `runtime: claude-session` SDK-native runtime (no tmux)

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src files, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes

### Reference
- [13_observability.md](13_observability.md) — `sac show-status` JSON contract
- [14_pane-actions.md](14_pane-actions.md) — Typed pane-mediated operations

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging

## Environment

- [30_env-vars.md](30_env-vars.md) — `SCITEX_*` env vars read at runtime

## 30-second start

```bash
pip install scitex-agent-container
sac start my-agent.yaml
sac show-status my-agent --json | jq .pane_state
sac attach my-agent           # Ctrl-B D to detach
```
