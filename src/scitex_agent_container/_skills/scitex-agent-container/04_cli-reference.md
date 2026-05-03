---
description: |
  [TOPIC] sac CLI reference
  [DETAILS] Lifecycle (start/stop/restart/show-status/show-logs), introspection (list-agents, find, mcp), action surface (actions, ingest-hook-event, recall), remote helpers (probe-network, render-attach), and MCP introspection. Long-form flag tables are in 10_cli.md.
tags: [scitex-agent-container-cli-reference]
---

# CLI Reference

Entry: `sac` (alias for `scitex-agent-container`).

```
sac [OPTIONS] COMMAND [ARGS]...
```

Global flags:
- `-h / --help` — show help
- `--help-recursive` — show help for the root command and every subcommand
- `--json` — emit structured JSON where supported (status / actions / events)

## Lifecycle (most-used)

| Command | Purpose |
|---|---|
| `sac start <agent>` | Start the agent. Daemon by default; add `--foreground` to stream stdio + block. Honors `spec.remote.host` (ssh dispatch) and `spec.a2a.port` (HTTP inbound endpoint). |
| `sac stop <agent>` | SIGTERM the runner; escalate to SIGKILL after 5 s. ssh-mediated for remote agents. |
| `sac restart <agent>` | Stop + start, preserving session_id resume. |
| `sac show-status [<agent>]` | Heartbeat + `sdk_session` block + last-action summary. With no arg, lists all registered agents. |
| `sac show-logs <agent>` | Rendered transcript from `session.jsonl` (user / assistant / result events). ssh-tails remote logs. |
| `sac attach <agent>` | (claude-code runtime only) Attach to the multiplexer session. |

Multi-target: `sac start a b c` works for daemon mode; `--foreground` is single-target only.

## Introspection

| Command | Purpose |
|---|---|
| `sac list-agents` | Registered agents on this host. |
| `sac find <capability>` | Search agents by capability label. |
| `sac inspect <agent>` | Live state: capture pane / heartbeat / quota. |
| `sac check <agent>` | Preflight checks (SSH, screen, sac-on-remote, python, disk). |
| `sac check-health <agent>` | Health-method poll (`multiplexer-alive` / `pane-prompt`). |
| `sac check-priority` | Singleton priority across the fleet. |

## Actions / events

| Command | Purpose |
|---|---|
| `sac actions <action>` | Run / query / aggregate agent-action attempts. |
| `sac ingest-hook-event` | Append a Claude Code hook event to the per-agent event log. |
| `sac recall <agent>` | Summarize the agent's session.jsonl. |
| `sac send-accept <agent>` | One-shot capture → classify → respond (auto-accept). |
| `sac start-auto-accept <agent>` | Start the auto-accept daemon. |

## Remote / SLURM helpers

| Command | Purpose |
|---|---|
| `sac probe-network` | WSL → fleet-hub connectivity probe. |
| `sac render-attach <agent>` | Print the `srun --pty` command for `slurm-tenant` agents. |
| `sac render-sbatch <agent>` | Print the sbatch wrapper for `runtime: slurm`. |
| `sac render-contributor-spec` | Materialize a contributor agent spec from the v3 template. |

## A2A protocol

| Command | Purpose |
|---|---|
| `sac a2a serve <yamls...>` | Run the A2A HTTP server (sidecar mode for `runtime: claude-code` agents). |
| `sac a2a list-agents <yamls...>` | List agents the server would expose. |

For `runtime: claude-session` agents the runner hosts `POST /v1/turn` itself — see [06_http-api.md](06_http-api.md).

## Build / install

| Command | Purpose |
|---|---|
| `sac build-image` | Build the container base image (docker / podman / apptainer). |
| `sac installation` | Bootstrap helpers for a new fleet host. |
| `sac install-post-merge-cron` | Add (or remove) the post-merge-pull crontab entry. |

## Other

| Command | Purpose |
|---|---|
| `sac mcp list-tools` | (no MCP servers bundled — sac agents spawn their own via `src_mcp.json`) |
| `sac account` | Manage stored Claude Code accounts. |
| `sac clean-registry` | Remove stale registry entries. |
| `sac reconcile-singletons` | Reconcile singleton agent placement across the fleet. |
| `sac list-python-apis` | Enumerate the public Python API. |

## See also

- [10_cli.md](10_cli.md) — long-form flag tables + Python-API mirror
- [03_python-api.md](03_python-api.md) — programmatic surface (mirrors the lifecycle commands)
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [13_observability.md](13_observability.md) — `show-status --json` contract
