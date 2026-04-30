---
name: agent-container-cli
description: CLI commands and Python API for scitex-agent-container. Both the long form (`scitex-agent-container ...`) and the short alias (`sac ...`) are installed by the package.
tags: [scitex-agent-container, scitex-package]
---

# scitex-agent-container CLI

Both the long form `scitex-agent-container` and the short alias `sac` are entry points to the same Click app. Examples below use `sac` for brevity.

## Lifecycle

```bash
sac start <config.yaml>          # Submit/launch one agent from YAML (dir-as-SSoT)
sac start --all                  # Start every agent under SCITEX_AGENT_CONTAINER_YAML_DIRS
sac start --no-preflight         # Skip SSH preflight checks (HPC where module load is needed)
sac start --force                # Stop existing instance first then start fresh
sac stop <name|yaml>             # Stop a running agent (or YAML path; resolves to name)
sac stop --all                   # Stop every registered agent
sac stop --force                 # Tolerate stale registry / ghost screen state
sac restart <name>               # Stop then start
sac cleanup                      # Remove stale registry entries (where the screen is already gone)
sac validate <config.yaml>       # Validate YAML against the v3 schema
```

## Inspection

```bash
sac list                         # All registered agents (table)
sac list --json                  # Machine-readable
sac list --capability X          # Filter by capability label
sac list --machine Y             # Filter by machine label
sac status [name]                # Rich status: pane state, hooks, listen ports, snapshot
sac status [name] --json         # Same, JSON
sac inspect <name>               # Live pane-state classification (idle/working/auth/...)
sac inspect <name> --json        # Same, JSON
sac logs <name> [-n LINES]       # Recent agent output (capture-pane / journalctl / tmux capture)
sac health <name>                # Run a health check on an agent
sac attach <name>                # Attach to the agent's multiplexer session (Ctrl-B D to detach)
sac find <capability>            # Find agents with a specific capability label across YAML roots
```

## SLURM

```bash
sac render-sbatch <yaml>         # Print the sbatch wrapper text (debug; doesn't submit)
sac render-attach <name>         # Print the srun --pty command that reattaches
```

For multi-tenant SLURM (`runtime: slurm-tenant`), see `09_slurm-tenant.md` and the companion `scitex-hpc reservations` CLI.

## A2A protocol (sidecar)

```bash
sac a2a serve <agent.yaml>...    # Foreground A2A server for one or more agents
sac a2a doctor <agent.yaml>      # Probe an agent's AgentCard endpoint, report health
```

Auto-launch is wired via `spec.a2a.port` — `sac start` spawns the A2A server as a sidecar subprocess after the multiplexer is up. See `07_a2a-protocol.md`.

## Build & deployment

```bash
sac build                        # Build container base image
sac check <yaml>                 # Run preflight checks (SSH reachability, claude on PATH, …)
sac probe-network                # Probe WSL → fleet-hub connectivity (todo#457)
```

## Operational tools

```bash
sac actions run <action> <agent>      # Execute a typed PaneAction (e.g. nonce-probe)
sac actions query --agent X --limit 5 # Query the host-wide attempt log
sac actions stats --agent X --since 1h # Aggregate stats
sac actions purge                     # Purge the attempt log
sac account                           # Manage stored Claude Code accounts (rotation)
sac quota-watch                       # Monitor quota and auto-rotate credentials
sac snapshot                          # Take a self-snapshot for AGENT, print as JSON
sac hook-event                        # Append a Claude Code hook event to the per-agent ring buffer
```

## Discoverability

```bash
sac list-python-apis [-v|-vv]    # List public Python APIs (v=docstrings, vv=full docs)
sac --help                       # Top-level help
sac <subcommand> --help          # Per-subcommand help
```

## Python API

```python
from scitex_agent_container import (
    agent_start, agent_stop, agent_restart,
    agent_status, agent_logs,
    Registry, load_config,
)

agent_start("path/to/agent.yaml")
status = agent_status("my-agent")
print(agent_logs("my-agent", lines=50))
```

The CLI is a thin wrapper over these — every command corresponds to a function in `scitex_agent_container.lifecycle` or related modules.

## Conventions

- **Noun-verb subcommand structure** for grouped operations (`sac actions run`, `sac a2a serve`). Single-word commands are top-level (`sac start`, `sac stop`).
- **`--json` always available** on inspection commands so dashboards can consume them.
- **`--force` is universal** for destructive ops — never silently overwrites without it.
- **Both `<name>` and `<yaml-path>` accepted** by `start`/`stop`/`restart`/`validate` — the CLI resolves yaml paths to agent names internally.

## See also

- `01_config-v3.md` — the YAML schema the CLI consumes
- `09_slurm-tenant.md` — multi-tenant SLURM runtime + `scitex-hpc reservations` CLI
- `40_troubleshooting.md` — common launch failures
