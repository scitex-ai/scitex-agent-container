---
description: |
  [TOPIC] sac CLI reference
  [DETAILS] Noun-verb tree (agent / auto-accept / check / render / peer / a2a / account / actions) + flat lifecycle verbs (start, stop, restart, attach, validate, recall, find). Long-form flag tables are in 10_cli.md.
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

The CLI follows the SciTeX noun-verb grammar (per
``general/03_interface_02_cli/02_subcommand-structure-noun-verb.md``):
multi-verb domains are noun-groups (``sac agent list``); single-action
verbs that take a positional are flat (``sac start <agent>``).

## Lifecycle (flat verbs — most-used)

| Command | Purpose |
|---|---|
| `sac start <agent>` | Start the agent. Daemon by default; add `--foreground` to stream stdio + block. Honors `spec.remote.host` (ssh dispatch) and `spec.a2a.port` (HTTP inbound endpoint). |
| `sac stop <agent>` | SIGTERM the runner; escalate to SIGKILL after 5 s. ssh-mediated for remote agents. |
| `sac restart <agent>` | Stop + start, preserving session_id resume. |
| `sac attach <agent>` | (claude-code runtime only) Attach to the multiplexer session. |
| `sac validate <yaml>` | Static validation of an agent YAML config. |
| `sac recall <agent>` | Summarize the agent's session.jsonl. |
| `sac find <capability>` | Search agents by capability label. |

Multi-target: `sac start a b c` works for daemon mode; `--foreground` is single-target only.

## `sac agent` — query / inspect (5 verbs)

| Command | Purpose |
|---|---|
| `sac agent list` | Registered agents on this host. |
| `sac agent status [<agent>]` | Heartbeat + `sdk_session` block + last-action summary. |
| `sac agent logs <agent>` | Rendered transcript from `session.jsonl`. ssh-tails remote logs. |
| `sac agent inspect <agent>` | Live state: capture pane / heartbeat / quota. |
| `sac agent snapshot <agent>` | Take a self-snapshot and emit JSON. |

## `sac check` — preflight / health / priority (3 verbs)

| Command | Purpose |
|---|---|
| `sac check preflight <yaml>` | SSH / screen / sac-on-remote / python / disk. |
| `sac check health <agent>` | Health-method poll (`multiplexer-alive` / `pane-prompt`). |
| `sac check priority <yaml>` | Singleton priority across the fleet. |

## `sac auto-accept` — Claude Code TUI handler (3 verbs)

| Command | Purpose |
|---|---|
| `sac auto-accept send <agent>` | One-shot capture → classify → respond. |
| `sac auto-accept start <agent>` | Start the auto-accept daemon (default 60 s tick). |
| `sac auto-accept stop <agent>` | Stop the auto-accept daemon. |

## `sac render` — emit runtime artifacts (3 verbs)

| Command | Purpose |
|---|---|
| `sac render sbatch <agent>` | Print the sbatch wrapper for `runtime: slurm`. |
| `sac render attach <agent>` | Print the `srun --pty` command for `slurm-tenant` agents. |
| `sac render contributor-spec` | Materialize a contributor agent spec from the v3 template. |

## `sac peer` — outbound A2A calls (2 verbs)

| Command | Purpose |
|---|---|
| `sac peer post-turn <agent> "<text>"` | Send one user turn to AGENT's `/v1/turn`. |
| `sac peer resolve-url <agent>` | Print the URL `post-turn` would POST to. |

## Actions / events (flat compound leaves)

| Command | Purpose |
|---|---|
| `sac actions <action>` | Run / query / aggregate agent-action attempts. |
| `sac ingest-hook-event` | Append a Claude Code hook event to the per-agent event log. |

## Remote helpers

| Command | Purpose |
|---|---|
| `sac probe-network` | WSL → fleet-hub connectivity probe. |

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

## Deprecated aliases

The following pre-noun-verb forms still work but print a stderr warning
and will be removed one release after the rename. Migrate to the new
paths above.

| Deprecated | Replacement |
|---|---|
| `sac list-agents` | `sac agent list` |
| `sac show-status` | `sac agent status` |
| `sac show-logs` | `sac agent logs` |
| `sac inspect` | `sac agent inspect` |
| `sac take-snapshot` | `sac agent snapshot` |
| `sac check-health` | `sac check health` |
| `sac check-priority` | `sac check priority` |
| `sac send-accept` | `sac auto-accept send` |
| `sac start-auto-accept` | `sac auto-accept start` |
| `sac stop-auto-accept` | `sac auto-accept stop` |
| `sac render-sbatch` | `sac render sbatch` |
| `sac render-attach` | `sac render attach` |
| `sac render-contributor-spec` | `sac render contributor-spec` |

Special case: bare `sac check <yaml>` (preflight) collides with the new
`check` group name — there is no alias; users must call
`sac check preflight <yaml>` explicitly.

## See also

- [10_cli.md](10_cli.md) — long-form flag tables + Python-API mirror
- [03_python-api.md](03_python-api.md) — programmatic surface (mirrors the lifecycle commands)
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [13_observability.md](13_observability.md) — `show-status --json` contract
