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
sac agents start <name|yaml>            # Launch one (or more) agents (dir-as-SSoT; name or YAML path)
sac agents start <name> --foreground    # Stream stdio + block until the turn finishes
sac agents stop <name|yaml>             # Stop a running agent (graceful SIGTERM → SIGKILL after 5 s)
sac agents restart <name>               # Stop then start, preserving session_id resume
sac agents delete <name> -y             # Stop, deregister, and remove the agent's dir
sac db clean                            # Sweep dead instances from state.db (replaces legacy registry clean)
```

## Inspection

```bash
sac agents list                         # All registered agents + liveness (table)
sac agents list --json                  # Machine-readable
sac agents list --capability X          # Filter by capability label
sac agents list --machine Y             # Filter by machine label
sac agents status [name]                # Per-agent status (heartbeat, session id, quota), or fleet view
sac agents status [name] --json         # Same, JSON
sac agents tail <name> [-n LINES]       # Render session.jsonl (user / assistant / tool / result events)
sac agents health <name>                # Run a health check (heartbeat freshness, restart policy)
sac agents recall <name>                # Human-readable session summary
sac agents check <name>                 # Preflight: validate yaml + probe runtime deps
sac agents find <capability>            # Find agents with a specific capability label across YAML roots
```

## Interact (resume an existing session)

```bash
sac agents send <name> "<prompt>"          # Resume the agent's session for one more turn
sac agents send <name> --key ESC           # Cancel the current turn (SIGINT to the runner pid)
sac agents send <name> --no-stream         # Buffer the reply instead of streaming
sac agents send <name> "..." -- --debug    # Anything after `--` is forwarded verbatim to claude
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

## A2A protocol (sidecar)

```bash
sac a2a serve <agent.yaml>...    # Foreground A2A server for one or more agents
sac a2a doctor <agent.yaml>      # Probe an agent's AgentCard endpoint, report health
```

Auto-launch is wired via `spec.a2a.port` — for SDK agents the runner hosts `POST /v1/turn` itself; `sac a2a serve` is the sidecar path for non-SDK runtimes. See `07_a2a-protocol.md`.

## Build & deployment

```bash
sac image build                        # Build container base image
sac agents check <yaml>                 # Run preflight checks (SSH reachability, claude on PATH, …)
sac host probe-hub               # Probe WSL → fleet-hub connectivity (DNS, gateway, TCP, HTTPS)
```

## Operational tools

```bash
sac accounts list                     # Stored accounts + active one + freshness column
sac accounts save <name>              # Snapshot current credentials for rotation
sac accounts sync-live                # Mirror the live credential into its matching store (idempotent; fails loud on stale/absent)
sac accounts watch-live               # Daemon: auto-sync the moment `claude /login` rewrites the live credential
sac accounts switch <name>            # Switch active credentials
sac accounts watch-quota              # Monitor quota and auto-rotate credentials
sac db clean                          # Sweep dead instances from state.db
sac db query --table instances        # Inspect state.db rows
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
from scitex_agent_container import agent, load_config, Registry

agent.start("my-agent")                 # name (dir-as-SSoT) or YAML path
status = agent.status("my-agent")
print(agent.logs("my-agent", lines=50))
```

The CLI is a thin wrapper over the namespaced API submodules
(`agent`, `db`, `host`, `image`, `account`, `skills`, `mcp`, `peer`).
Run `sac list-python-apis -vv` for the full signature tree.

## Conventions

- **Noun-verb subcommand structure** for grouped operations (`sac agents start`, `sac a2a serve`, `sac db query`).
- **`--json` always available** on inspection commands so dashboards can consume them.
- **Both `<name>` and `<yaml-path>` accepted** by `start`/`stop`/`restart` — the CLI resolves yaml paths to agent names internally.

## See also

- `01_config-v3.md` — the YAML schema the CLI consumes
- `40_troubleshooting.md` — common launch failures
