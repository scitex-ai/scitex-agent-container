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

Declarative lifecycle management for AI coding agents (Claude Code,
Cursor, Aider). Define an agent in YAML, launch it in a
tmux (default) or screen session locally or on a remote host via SSH,
and observe it through a rich non-agentic status surface.

## What the package ships

| Surface | Location |
|---|---|
| Python API | `scitex_agent_container` (`AgentConfig`, `load_config`, `validate_config`, `agent_start`, `agent_stop`, `agent_restart`, `agent_status`, `agent_logs`, `Registry`) |
| CLI entry points | `scitex-agent-container`, `sac` (see [10_cli.md](10_cli.md)) |
| MCP servers | None bundled — agents spawn their own via `src_mcp.json` |
| Runtimes | `runtimes/tmux.py`, `runtimes/screen.py`, `runtimes/claude_code.py`, `runtimes/apptainer.py`, `runtimes/docker.py`, `runtimes/sbatch_spartan.py`, `runtimes/ssh_remote.py` |
| PaneActions | `actions/nonce_probe.py`, `actions/compact.py` (typed, logged via `action_store` to `~/.scitex/agent-container/actions.db`) |
| Observability | `agent_meta.collect_rich()`, `event_log`, `snapshot`, `hooks.hook_event`, `liveness_probe`, `quota_watch` |
| Config format | v3 `scitex-agent-container/v3` (current); v2 still supported |

## Sub-skills

### Core
- [01_config-v3.md](01_config-v3.md) — v3 config format (current), apiVersion `scitex-agent-container/v3`, dir-as-SSoT, auto-derived fields, src_* files. v2 + `metadata.name` are explicitly rejected by the loader.
- [02_multiplexer.md](02_multiplexer.md) — tmux vs screen, capture-pane, send-keys
- [03_auto-accept.md](03_auto-accept.md) — Modular prompt handlers, extending, diagnostics
- [04_resource-management.md](04_resource-management.md) — Resource management
- [05_resource-heartbeat.md](05_resource-heartbeat.md) — Resource heartbeat
- [06_env-injection-ports.md](06_env-injection-ports.md) — Four env-injection ports (yaml.env / src_mcp.json env / src_env / hooks) with reach + decision tree
- [07_a2a-protocol.md](07_a2a-protocol.md) — Native A2A protocol support (`sac a2a serve`); standalone orochi-free agents with echo / claude_cli / exec handlers
- [08_templates.md](08_templates.md) — Six minimal pattern templates (local / docker / apptainer / ssh / ssh-slurm / mcp) under `config/templates/` and real-world configs under `config/examples/`
- [09_slurm-tenant.md](09_slurm-tenant.md) — `runtime: slurm-tenant` for many agents in one allocation; pairs with `scitex-hpc reservations book --tmux-server sac`. Architecture rationale (cgroup/tmux), workflow, troubleshooting.

### Workflows
- [10_cli.md](10_cli.md) — CLI commands and Python API
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment, src file copying, venv
- [12_wsl-connectivity.md](12_wsl-connectivity.md) — WSL connectivity notes

### Lessons
- [40_troubleshooting.md](40_troubleshooting.md) — Common issues and debugging


## Environment

- [30_env-vars.md](30_env-vars.md) — SCITEX_* env vars read by scitex-agent-container at runtime

## 30-second start

```bash
pip install scitex-agent-container
scitex-agent-container start my-agent.yaml
scitex-agent-container status my-agent --json | jq .pane_state
scitex-agent-container attach my-agent           # Ctrl-B D to detach
```

## Observability contract

`status <name> --json` merges the registry entry with `agent_meta.collect_rich()`
and `event_log.summarize()`. Downstream orchestrators (e.g. scitex-orochi)
consume that JSON — no direct coupling. Every field is best-effort; failures
leave the default (`""`, `0`, `[]`) rather than raising. See the README's
"Rich Status" table for the full field list.

## PaneActions

Typed, logged vocabulary for pane-mediated operations. Each action subclasses
`PaneAction` and implements `snapshot` / `precheck` / `send` / `is_complete`.
`run_action` classifies every attempt (`success`, `precondition_fail`,
`send_error`, `completion_timeout`, `skipped_by_policy`) and writes the row to
the host-wide SQLite store at `~/.scitex/agent-container/actions.db`.
Built-ins: `NonceProbeAction` (functional liveness) and `CompactAction`
(context-window compaction with drop-verification). See
`scitex-agent-container actions --help`.
