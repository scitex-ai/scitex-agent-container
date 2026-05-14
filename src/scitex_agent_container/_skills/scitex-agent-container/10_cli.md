---
description: |
  [TOPIC] scitex-agent-container CLI
  [DETAILS] CLI commands and Python API for scitex-agent-container. Both the long form (`scitex-agent-container ...`) and the short alias (`sac ...`) are installed by the package..
tags: [scitex-agent-container-cli]
---

# scitex-agent-container CLI

Both the long form `scitex-agent-container` and the short alias `sac` are entry points to the same Click app. Examples below use `sac` for brevity.

## Lifecycle

```bash
sac agent start <config.yaml>          # Submit/launch one agent from YAML (dir-as-SSoT)
sac agent start --all                  # Start every agent under SCITEX_AGENT_CONTAINER_YAML_DIRS
sac agent start --no-preflight         # Skip SSH preflight checks (HPC where module load is needed)
sac agent start --force                # Stop existing instance first then start fresh
sac agent stop <name|yaml>             # Stop a running agent (or YAML path; resolves to name)
sac agent stop --all                   # Stop every registered agent
sac agent stop --force                 # Tolerate stale registry / ghost screen state
sac agent restart <name>               # Stop then start
sac registry clean                      # Remove stale registry entries (where the screen is already gone)
sac agent validate <config.yaml>       # Validate YAML against the v3 schema
```

## Inspection

```bash
sac list                         # All registered agents (table)
sac list --json                  # Machine-readable
sac list --capability X          # Filter by capability label
sac list --machine Y             # Filter by machine label
sac agent status [name]                # Rich status: pane state, hooks, listen ports, snapshot
sac agent status [name] --json         # Same, JSON
sac agent inspect <name>               # Live pane-state classification (idle/working/auth/...)
sac agent inspect <name> --json        # Same, JSON
sac agent logs <name> [-n LINES]       # Recent agent output (capture-pane / journalctl / tmux capture)
sac agent health <name>                # Run a health check on an agent
sac agent attach <name>                # Attach to the agent's multiplexer session (Ctrl-B D to detach)
sac agent find <capability>            # Find agents with a specific capability label across YAML roots
```

## SLURM

```bash
sac template render-sbatch <yaml>         # Print the sbatch wrapper text (debug; doesn't submit)
sac template render-attach <name>         # Print the srun --pty command that reattaches
```

For multi-tenant SLURM (`runtime: slurm-tenant`), see `09_slurm-tenant.md` and the companion `scitex-hpc reservations` CLI.

## Interact (resume an existing session)

```bash
sac agent send <name> "<prompt>"          # Resume the agent's session for one more turn
sac agent send <name> --key ESC           # Cancel the current turn (SIGINT to the runner pid)
sac agent send <name> --no-stream         # Buffer the reply instead of streaming
sac agent send <name> "..." -- --debug    # Anything after `--` is forwarded verbatim to claude
```

Reads `session_id` from the per-agent state dir and shells out to `claude --resume <sid> -p ...` inside the agent's `workdir`. See `15_claude-session.md` for the long-lived alternative that keeps the SDK client open across turns.

## sac listen (HTTP/JSON control plane)

```bash
sac listen                                # Boot the local /v1/sac/ server (loopback only by default)
sac listen --bind 127.0.0.1:7878          # Custom bind
sac listen --print-token                  # Echo the bearer token & exit
```

When running, exposes (bearer-token authenticated):

| Route | Purpose |
|---|---|
| `GET  /v1/sac/health` | Liveness; public |
| `GET  /agents` | List local registry |
| `GET  /agents/<name>/status` | Spec path, workdir, session_id |
| `GET  /agents/<name>/card` | A2A-compatible AgentCard |
| `POST /agents` | Start (body: `{name}` or `{name, spec}` for inline-spec register-and-start) |
| `POST /agents/<name>/send` | One turn — buffered JSON by default; `Accept: text/event-stream` → SSE frames |
| `DELETE /agents/<name>` | SIGTERM the runner pid |

## sac channel (local agent ↔ agent)

```bash
sac channel send <to> "<msg>" --from <id>   # POST a channel-wrapped turn to a local agent via sac listen
```

## A2A protocol (sidecar)

```bash
sac a2a serve <agent.yaml>...    # Foreground A2A server for one or more agents
sac a2a doctor <agent.yaml>      # Probe an agent's AgentCard endpoint, report health
```

Auto-launch is wired via `spec.a2a.port` — `sac agent start` spawns the A2A server as a sidecar subprocess after the multiplexer is up. See `07_a2a-protocol.md`.

## Build & deployment

```bash
sac image build                        # Build container base image
sac agent check <yaml>                 # Run preflight checks (SSH reachability, claude on PATH, …)
sac network probe                # Probe WSL → fleet-hub connectivity (todo#457)
```

## Operational tools

```bash
sac actions run <action> <agent>      # Execute a typed PaneAction (e.g. nonce-probe)
sac actions query --agent X --limit 5 # Query the host-wide attempt log
sac actions stats --agent X --since 1h # Aggregate stats
sac actions purge                     # Purge the attempt log
sac account                           # Manage stored Claude Code accounts (rotation)
sac quota watch                       # Monitor quota and auto-rotate credentials
sac agent take-snapshot                          # Take a self-snapshot for AGENT, print as JSON
sac event ingest                        # Append a Claude Code hook event to the per-agent ring buffer
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

- **Noun-verb subcommand structure** for grouped operations (`sac actions run`, `sac a2a serve`). Single-word commands are top-level (`sac agent start`, `sac agent stop`).
- **`--json` always available** on inspection commands so dashboards can consume them.
- **`--force` is universal** for destructive ops — never silently overwrites without it.
- **Both `<name>` and `<yaml-path>` accepted** by `start`/`stop`/`restart`/`validate` — the CLI resolves yaml paths to agent names internally.

## See also

- `01_config-v3.md` — the YAML schema the CLI consumes
- `09_slurm-tenant.md` — multi-tenant SLURM runtime + `scitex-hpc reservations` CLI
- `40_troubleshooting.md` — common launch failures
