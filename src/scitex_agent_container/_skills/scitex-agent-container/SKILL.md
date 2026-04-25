---
name: scitex-agent-container
description: Declarative YAML-based AI agent lifecycle management with tmux/screen, SSH remote deployment, modular auto-accept, and live state inspection.
---

# scitex-agent-container

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

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src file copying, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging


## Environment

- [30_env-vars.md](30_env-vars.md) — SCITEX_* env vars read by scitex-agent-container at runtime
