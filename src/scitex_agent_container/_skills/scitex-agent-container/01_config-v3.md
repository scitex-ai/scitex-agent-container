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
    └── to_home/        # optional; auto-discovered next to spec.yaml; mirrors $HOME
        ├── CLAUDE.md         # → $HOME/CLAUDE.md   (marker-protected)
        ├── .mcp.json         # → $HOME/.mcp.json   (full overwrite)
        ├── .env              # → $HOME/.env        (mode 0600)
        ├── state.md          # → $HOME/state.md    (marker-protected)
        └── .claude/
            ├── hooks/         # → $HOME/.claude/hooks/
            └── skills/        # → $HOME/.claude/skills/
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

### `spec.claude` fields

| Field | Type | Purpose |
|---|---|---|
| `model` | alias or full ID | `opus` / `sonnet` (default) / `haiku` (+ `[1m]` for 1M context), or a full ID like `claude-opus-4-7`. May also sit at `spec.model` (top level). Abbreviated IDs missing version digits (`claude-opus[1m]`) are rejected at validate-time. |
| `session` | enum | `continue-or-new` (default) \| `continue` \| `new` \| `resume` |
| `resume_id` | string | Explicit session UUID for `session: resume` |
| `continue_max_age_minutes` | int | Only resume if `session.jsonl` is newer than N minutes |
| `flags[]` | list | Extra flags appended to the `claude` invocation |
| `channels[]` | list | MCP push channels (`server:<name>` / `plugin:<id>@<v>`) |
| `auto_accept` | bool (default `true`) | Auto-confirm TUI permission prompts |
| `account` | string | Pin this agent to a stored OAuth account (`sac accounts` store-name). Credentials are **boot-copied** into the agent state dir, not live-bound. Mutually exclusive with `provider`. See [26_credentials-rotation.md](26_credentials-rotation.md). |
| `provider` | `{ base_url, auth_token_env }` | Point the SDK at any Anthropic-compatible backend (e.g. DeepSeek). `base_url` is the endpoint; `auth_token_env` is the **name** of the host env var holding the key (never the key value). Mutually exclusive with `account`; relaxes the `claude-*` model-alias check. See ADR-0011. |
| `raw_options` | dict | Escape hatch — splatted into `ClaudeAgentOptions(**raw_options)` |

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

## `to_home/` deploy pipeline (ADR-0006)

A sibling directory named `to_home/` (override path with
`spec.to_home:`, default `./to_home`) is materialized into the agent's
container `$HOME` (= `runtime/<name>/home/`) at `sac agents start` time.
Every path under `to_home/` lands at the same relative path under
`$HOME`. `CLAUDE.md` / `state.md` get a marker-protected merge; `.env`
gets mode 0600; everything else is a full overwrite. `${VAR}` and
`${metadata.name}` are interpolated in text files. A shared baseline
`to_home/` (`<agents_dir>/_base/to_home`, override `$SAC_TO_HOME_BASELINE`)
is applied first; the per-agent `to_home/` overlays on top.

| Source | Destination | Mode | Semantics |
|---|---|---|---|
| `to_home/CLAUDE.md` | `$HOME/CLAUDE.md` | 0644 | Marker-protected; preserves user tail past the End marker |
| `to_home/.mcp.json` | `$HOME/.mcp.json` | 0644 | Full overwrite |
| `to_home/.env` | `$HOME/.env` | **0600** | Full overwrite; sourceable by spawned shells |
| `to_home/state.md` | `$HOME/state.md` | 0644 | Marker-protected (handover snapshot) |
| `to_home/.claude/<x>/` | `$HOME/.claude/<x>/` | copy | `hooks/`, `skills/`, `commands/`, `agents/`, … |

The legacy `dot_claude/` layout was removed (ADR-0006). A spec that
still ships a `dot_claude/` dir is rejected with an error pointing at
`to_home/`.

See `06_env-injection-ports.md` for the four distinct env-injection ports and when to use each.

## Migration from v2 → v3

If you have legacy YAMLs:

1. Change `apiVersion: scitex-agent-container/v2` → `apiVersion: scitex-agent-container/v3`.
2. Delete the `metadata.name:` field. The agent name now comes from the parent directory; ensure the YAML lives at `<name>/<name>.yaml`.
3. If your YAML was at a flat path like `~/.scitex/agent-container/agents/foo.yaml`, move it to `~/.scitex/agent-container/agents/foo/foo.yaml`.
4. `sac agents check <new-path>` to confirm it parses (preflight).

## See also

- `08_templates.md` — six minimal pattern templates under `examples/agent-templates/`
- `06_env-injection-ports.md` — yaml.env vs to_home/.mcp.json env vs to_home/.env vs hooks
