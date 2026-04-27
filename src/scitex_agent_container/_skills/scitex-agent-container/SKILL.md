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
---

# scitex-agent-container

> **Interfaces:** Python ⭐⭐ · CLI ⭐⭐⭐ (primary) · MCP ⭐ · Skills ⭐⭐ · Hook — · HTTP —

Declarative agent deployment. Define agents in YAML, launch them in tmux/screen sessions locally or on remote hosts via SSH.

## Sub-skills

### Core
- [01_config-v2.md](01_config-v2.md) — v2 config format, auto-derived fields, src files
- [02_multiplexer.md](02_multiplexer.md) — tmux vs screen, capture-pane, send-keys
- [03_auto-accept.md](03_auto-accept.md) — Modular prompt handlers, extending, diagnostics
- [04_resource-management.md](04_resource-management.md) — Resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — Resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — Four env-injection ports (yaml.env / src_mcp.json env / src_env / hooks) with reach + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — Native A2A protocol support (`sac a2a serve`); standalone orochi-free agents with echo / claude_cli / exec handlers
- [08_templates.md](08_templates.md) — Six minimal pattern templates (local / docker / apptainer / ssh / ssh-slurm / mcp) under `config/templates/` and real-world configs under `config/examples/`

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src file copying, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging


## Environment

- [30_env-vars.md](30_env-vars.md) — SCITEX_* env vars read by scitex-agent-container at runtime
