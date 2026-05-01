---
name: env-injection-ports
description: Env-injection ports for agents — see file body for details.
tags: [scitex-agent-container, scitex-package]
---

# Env-injection ports for agents

Agents have **four distinct env-injection ports** with different reach and persistence.
Pick the right one for the job — they are not interchangeable.

| Port | File | Reaches | Persistent on disk? | Code path |
| --- | --- | --- | --- | --- |
| 1. **Agent shell env** | `<agent>.yaml` → `spec.env: {KEY: val}` | the multiplexer session running Claude (`os.environ`, Bash tool, child procs) | no — exports inline | `runtimes/claude_code.py::_build_env_exports` |
| 2. **MCP server env** | `src_mcp.json` → `mcpServers.<name>.env` | only the spawned MCP server process | no | MCP launcher reads it directly |
| 3. **Workspace dotenv** | `src_env` next to agent YAML → `{workdir}/.env` | anything the agent (or its subprocesses) chooses to source | **yes — file in workdir** | `runtimes/src_files.py::deploy_src_env` (see `src_CLAUDE.md` / `src_mcp.json` siblings for the pattern) |
| 4. **Hook env** | `<agent>.yaml` → `spec.hooks.pre_start: [cmd]` + `_run_hooks(extra_env=...)` | only the hook subprocess; **does not propagate back to the agent** | no | `lifecycle.py::_run_hooks`, `hooks.py` |

The `src_*` family (`src_CLAUDE.md`, `src_mcp.json`, `src_env`) is sac's generic file-deploy convention: a file named `src_X` next to the agent YAML is materialized into the workspace at agent start, with `${VAR}` and `${metadata.name}` interpolation. sac does not know about any specific project — projects contribute the content.

## Decision tree

- Need it inside Claude's shell for the running session? → **port 1** (`spec.env`).
- Need it inside an MCP server? → **port 2** (`src_mcp.json env`).
- Need it available to *any* subprocess the agent spawns later (cron, ssh-launched commands, fresh shells)? → **port 3** (`src_env` → workspace `.env`).
- Need to *do something at lifecycle moments* (notify, cleanup)? → **port 4** (`hooks`). Hooks fire-and-forget; cannot mutate agent env.

## Value resolution

`spec.env` values support:
- `~` prefix → expanded to `$HOME`
- `${VAR}` → resolved from `os.environ` at launch (sac side, not agent side)
- `${metadata.name}` → substituted with the agent id

`src_mcp.json` env values use the same `${VAR}` syntax (resolved by the MCP launcher in the parent shell at spawn time).

## Why hooks can't inject

`_run_hooks` builds `extra_env` and passes it to `subprocess.run`. The hook process has it; nothing propagates upward. So hooks are useful for *side effects* (write a file, notify a service) — never for handing a secret to the agent.

## Pattern: secrets in the dotfiles, paths in env

Don't put raw tokens in YAML/JSON committed to git. Pattern:

1. Token file at `~/.bash.d/secrets/010_scitex/.../<id>.<purpose>-token` (rides with dotfiles git, syncs across hosts).
2. Inject the **path** via port 1 or 2 (e.g. `SCITEX_OROCHI_GITEA_TOKEN_PATH=~/.bash.d/secrets/.../${SCITEX_AGENT_NAME}.gitea-token`), or read the file in the parent shell and inject the value via `${VAR}`.
3. Consumer reads file on demand.

Path-in-env is safer than value-in-env (`/proc/<pid>/environ` exposure), but value-in-env is fine when the consumer is short-lived (MCP server process).
