---
description: |
  [TOPIC] scitex-agent-container CLI
  [DETAILS] CLI commands and Python API for scitex-agent-container. Both the long form (`scitex-agent-container ...`) and the short alias (`sac ...`) are installed by the package..
tags: [scitex-agent-container-cli]
---

# scitex-agent-container CLI

Both the long form `scitex-agent-container` and the short alias `sac` are entry points to the same Click app. Examples below use `sac` for brevity.

## Cards store credentials

`sac agents sync-cards-store-credential --agent worker --to peer-a --dry-run`
reads the private source passfile and sends its credential to each trusted peer
via SSH stdin for validation, without passfile writes. Replace `--dry-run` with
`--yes` (`-y`, `--apply`) to write after all peers validate. The flags conflict.
Cross-host apply is not transactional. See [credential synchronization](../../../../docs/credentials.md#cards-store-credential-synchronization).

## Lifecycle

```bash
sac agents start <name|yaml>            # Launch one (or more) agents (dir-as-SSoT; name or YAML path)
sac agents start <name> --foreground    # Stream stdio + block until the turn finishes
sac agents stop <name|yaml>             # Stop a running agent (graceful SIGTERM → SIGKILL after 5 s)
sac agents restart <name>               # Stop then start, preserving session_id resume
sac agents rename <old> <new> --dry-run # Show every location a rename would touch (exact; changes nothing)
sac agents rename <old> <new> -y        # Rename EVERYWHERE, atomically (rolls back on any failure)
sac agents delete <name> -y             # Stop, deregister, and remove the agent's dir
sac store clean                            # Sweep dead instance rows (replaces legacy registry clean)
```

### `rename` — why it is a verb and not a `mv`

An agent writes its own name into six places on disk plus the shared task
board. `rename` moves all of them together, or none:

| # | Location |
|---|---|
| 1 | spec dir — `~/.scitex/agent-container/agents/<name>/` |
| 2 | the spec's self-references — `metadata.labels.project` / `.purpose`, `spec.workdir`, the `--overlay` path, and `SCITEX_TODO_AGENT_ID` |
| 3 | overlay dir — `.../containers/overlays/<name>/` |
| 4 | runtime + state dir — `.../runtime/<name>/` (bound into the container at `/state/<name>`) |
| 5 | registry entry — `.../runtime/registry/<name>.json` |
| 6 | shared PostgreSQL state-store records keyed by the agent name (identity **and** history) |
| 7 | **task cards** — reassigned via scitex-todo's own `reassign_task` |

Step 7 is the reason the verb exists. The board knows an agent by
`SCITEX_TODO_AGENT_ID`. Change it without migrating the cards and every card
that agent owns is **orphaned** — it can no longer see its own work, and
nothing tells you.

The agent must be **stopped** (renaming a live agent's workdir and overlay out
from under it is unsafe); `rename` refuses otherwise and prints the `stop`
command. Every step records its inverse, so any failure — including a partial
card migration — rolls the whole rename back.

`-y`/`--yes` is **required to apply**: without it the rename is refused (exit 2).
It never prompts, so it cannot hang under cron, CI, or an agent's non-tty shell.

`$SCITEX_AGENT_CONTAINER_ROOT` overrides the root all seven locations derive
from.

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
sac agents show-identity                # Inspect injected identity, spec and runtime context
sac agents show-identity --json         # JSON facts; secret values are never printed
```

## Interact (send a follow-up)

```bash
sac agents send --yes <name> "<prompt>"          # Follow-up to a live agent
```

Uses HTTP to the live agent, directly or through host listen; never a host-side `claude --resume`. A valid HTTP 202 means accepted, still pending: follow the returned exchange poll hint before claiming completion. Source and controls: `cli_pkg/send_cmds.py`, `tests/scitex_agent_container/cli_pkg/test_send_cmds.py`.

## sac listen (HTTP/JSON control plane)

`listen` is a command group like `agents` / `db` / `host`. Its
four verbs are the whole lifecycle:

```bash
sac listen start --yes                    # Boot the daemon (loopback by default)
sac listen start --yes --bind 127.0.0.1:7979    # Bind
sac listen start --yes --print-token            # Print token and exit
sac listen status                         # Health report; exit 1 if not serving
sac listen status --json                  # Machine-readable status envelope
sac listen stop --yes                     # Stop (idempotent; exit 0 if already down)
sac listen stop --yes --json              # Machine-readable result envelope
sac listen restart --yes                  # Self-healing restart
```

Options may be given on the verb (`sac listen start --yes --bind …`) or on the
group (`sac listen --bind … start --yes`); the verb wins.

`restart` is the deterministic incident-recovery verb: it clears a stale pidfile, force-kills an untracked remnant still holding the port (the "curl hangs forever" case), then relaunches and health-probes — failing loud (non-zero, `ERROR:` naming the real cause) if the daemon can't be brought up. `status` is the one-command diagnosis. `stop` is `restart`'s stop half on its own — they share one implementation (`_listen._stop.stop_listen`), so they cannot drift.

> **DEPRECATED — bare `sac listen`.** It still BOOTS the daemon (the systemd
> unit and default direct respawn still invoke it bare), but prints a
> deprecation warning to stderr. The jobs provider deliberately excludes
> listen to avoid a second supervisor (ADR 0022). Use
> **`sac listen start --yes`**. The warning still names the historical **v0.23.0**
> removal target; removal has not been implemented. Existing launchers must
> migrate before bare startup can be removed. See `cli_pkg/listen_cmds.py`
> and `tests/scitex_agent_container/cli_pkg/test_listen_cmds.py` for the
> compatibility behavior and its regression controls.

When running, exposes (bearer-token authenticated, except the public health route):

| Route | Purpose |
|---|---|
| `GET  /v1/health` | Liveness; public (unauthenticated) |
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
sac accounts sync-live --yes                # Mirror the live credential into its matching store (idempotent; fails loud on stale/absent)
sac accounts watch-live               # Daemon: auto-sync the moment `claude /login` rewrites the live credential
sac accounts switch <name>            # Switch active credentials
sac accounts watch-quota              # Monitor quota and auto-rotate credentials
sac store clean                          # Sweep dead instance rows
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

- **Noun-verb subcommand structure** for grouped operations (`sac agents start`, `sac a2a serve`, `sac store clean`).
- **JSON:** use `--json` where advertised; policy emits JSON decisions directly.
- **Both `<name>` and `<yaml-path>` accepted** by `start`/`stop`/`restart` — the CLI resolves yaml paths to agent names internally.

## See also

- `01_config-v3.md` — the YAML schema the CLI consumes
- `40_troubleshooting.md` — common launch failures

## Policy projection preview

`sac worktree policy generate-projections --dry-run` previews arguments, without granting edit permission.
