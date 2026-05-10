---
description: |
  [TOPIC] v3 Config Format
  [DETAILS] v3 YAML config format — apiVersion scitex-agent-container/v3, dir-as-SSoT (agent name from parent directory, not metadata.name), auto-derived fields, src_*.<ext> file deployment..
tags: [scitex-agent-container-config-v3]
---

# v3 Config Format

## `apiVersion: scitex-agent-container/v3`

The current and only accepted apiVersion. The v3 loader **rejects**:

- `apiVersion: scitex-agent-container/v2` (or earlier) — fails validation with a clear "must be one of ('scitex-agent-container/v3',)" error.
- `metadata.name` field — agent name is derived from the YAML's parent directory (dir-as-SSoT). Including `metadata.name` triggers an error pointing the user at the dir-as-SSoT layout.

### Layout (dir-as-SSoT)

```
<agent-root>/
└── <name>/
    ├── <name>.yaml      # ← agent name comes from this directory
    ├── src_CLAUDE.md    # → deployed to <workdir>/CLAUDE.md
    ├── src_mcp.json     # → deployed to <workdir>/.mcp.json
    └── src_env          # → deployed to <workdir>/.env (mode 0600)
```

`<agent-root>` is one of:
- `~/.scitex/agent-container/agents/`
- Any colon-separated directory in `$SCITEX_AGENT_CONTAINER_YAML_DIRS`

### Minimal example

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:                 # optional; descriptive only
    role: worker
    machine: local
spec:
  runtime: claude-code    # claude-code | slurm | slurm-tenant
  model: opus[1m]
  multiplexer: tmux       # tmux (default) or screen

  claude:
    flags:
      - --dangerously-skip-permissions
    session: continue-or-new   # continue-or-new (default) | continue | new | resume

  skills:
    required: [scitex]

  health:
    enabled: true
    interval: 60
    method: multiplexer-alive

  restart:
    policy: on-failure
    max_retries: 3
```

## Auto-derived fields

The v3 loader fills in defaults from the agent name (parent-directory stem):

| Field | Auto-derived value |
|---|---|
| `screen_name` | the agent name itself (used for tmux/screen session) |
| `workdir` | `~/.scitex/agent-container/runtime/workspaces/<name>/` |
| `env.SCITEX_AGENT_CONTAINER_NAME` | `<name>` |
| `env.SCITEX_AGENT_CONTAINER_AGENT` | `<name>` |
| `env.SCITEX_AGENT_CONTAINER_ID` | `<name>` |
| `hooks.pre_start` | a `mkdir -p <workdir>` shim |

You can override any of these by setting them explicitly in the YAML. The auto-derivation is just a bottom layer of the resolution cascade.

## `src_*` file-deploy pipeline

Sibling files named `src_<basename>` next to the agent YAML are materialized into the workspace at `sac agent start` time, with `${VAR}` and `${metadata.name}` interpolation:

| Source | Destination | Mode |
|---|---|---|
| `src_CLAUDE.md` | `<workdir>/CLAUDE.md` | 0644 |
| `src_mcp.json` | `<workdir>/.mcp.json` | 0644 |
| `src_env` | `<workdir>/.env` | 0600 (sourceable by spawned shells, cron jobs, ssh-launched commands) |

Generic rule: any sibling file matching `src_X` is copied to `<workdir>/X` (the prefix is stripped). See `06_env-injection-ports.md` for the four distinct env-injection ports and when to use each.

## Migration from v2 → v3

If you have legacy YAMLs:

1. Change `apiVersion: scitex-agent-container/v2` → `apiVersion: scitex-agent-container/v3`.
2. Delete the `metadata.name:` field. The agent name now comes from the parent directory; ensure the YAML lives at `<name>/<name>.yaml`.
3. If your YAML was at a flat path like `~/.scitex/agent-container/agents/foo.yaml`, move it to `~/.scitex/agent-container/agents/foo/foo.yaml`.
4. `sac agent validate <new-path>` to confirm it parses.

## See also

- `08_templates.md` — six minimal pattern templates under `examples/agent-templates/`
- `06_env-injection-ports.md` — yaml.env vs src_mcp.json env vs src_env vs hooks
- `09_slurm-tenant.md` — multi-tenant `runtime: slurm-tenant` and `slurm.reservation`
