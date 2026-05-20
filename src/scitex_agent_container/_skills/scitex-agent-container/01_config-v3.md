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
    ├── spec.yaml       # ← agent name comes from this directory
    └── dot_claude/     # optional; auto-discovered next to spec.yaml
        ├── CLAUDE.md    # → <workdir>/CLAUDE.md  (marker-protected)
        ├── .mcp.json    # → <workdir>/.mcp.json  (per-server merge)
        ├── .env         # → <workdir>/.env       (mode 0600)
        ├── state.md     # → <workdir>/state.md
        └── commands/    # → <workdir>/.claude/commands/   (mirror)
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
  runtime: apptainer      # apptainer (only accepted value)
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

## `dot_claude/` deploy pipeline (replaces the legacy `src_*` siblings)

A sibling directory named `dot_claude/` (override path with
`spec.dot_claude:`) is materialized into the workspace at
`sac agent start` time. Four well-known leaves get special handling
(marker protocol, per-server merge, mode 0600); everything else
mirrors verbatim into `<workdir>/.claude/`. `${VAR}` and
`${metadata.name}` are interpolated.

| Source | Destination | Mode | Semantics |
|---|---|---|---|
| `dot_claude/CLAUDE.md` | `<workdir>/CLAUDE.md` | 0644 | Marker-protected; preserves user tail past the End marker |
| `dot_claude/.mcp.json` | `<workdir>/.mcp.json` | 0644 | Per-server replace; workspace-only servers preserved |
| `dot_claude/.env` | `<workdir>/.env` | **0600** | Full overwrite; sourceable by spawned shells |
| `dot_claude/state.md` | `<workdir>/state.md` | 0644 | Full overwrite (handover snapshot) |
| `dot_claude/<other>/` | `<workdir>/.claude/<other>/` | copy | Generic mirror — `commands/`, `skills/`, `hooks/`, `agents/`, … |

See `06_env-injection-ports.md` for the four distinct env-injection ports and when to use each.

## Migration from v2 → v3

If you have legacy YAMLs:

1. Change `apiVersion: scitex-agent-container/v2` → `apiVersion: scitex-agent-container/v3`.
2. Delete the `metadata.name:` field. The agent name now comes from the parent directory; ensure the YAML lives at `<name>/<name>.yaml`.
3. If your YAML was at a flat path like `~/.scitex/agent-container/agents/foo.yaml`, move it to `~/.scitex/agent-container/agents/foo/foo.yaml`.
4. `sac agent validate <new-path>` to confirm it parses.

## See also

- `08_templates.md` — six minimal pattern templates under `examples/agent-templates/`
- `06_env-injection-ports.md` — yaml.env vs dot_claude/.mcp.json env vs dot_claude/.env vs hooks
