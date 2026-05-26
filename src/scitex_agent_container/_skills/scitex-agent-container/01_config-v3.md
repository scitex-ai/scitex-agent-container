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
    session: continue   # continue (default) | new-session | resume
                        # (legacy aliases: continue-or-new→continue, new→new-session)

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

## `spec.claude` — model, session, OAuth account, provider override

Beyond `model` / `flags` / `session`, two fields control which backend
the agent's SDK session authenticates against:

```yaml
spec:
  claude:
    model: opus[1m]

    # OAuth account pinning. Name from `sac accounts list`. The runtime
    # COPIES that account's .credentials.json into the agent's state dir
    # at start (frozen boot-copy, bound RW so ~1h token refresh works) —
    # so two agents pinned to two accounts never fight one mount.
    # "" (default) = the host's live ~/.claude/.credentials.json.
    # Changing it needs `sac agent restart` to re-copy the snapshot.
    account: max-personal

    # Vendor-agnostic backend override. Runs the session against an
    # Anthropic-SDK-compatible backend (DeepSeek, a gateway, …) on a
    # never-expiring API key instead of Anthropic OAuth — cheap bulk
    # fleet work without burning Max quota. Mutually exclusive with
    # `account` (an API-key backend needs no OAuth; declaring both is a
    # config error rejected at start).
    provider:
      base_url: https://api.deepseek.com/anthropic  # required
      auth_token_env: DEEPSEEK_API_KEY              # NAME of host env var (not the key)
```

With a `provider` set, the `model` is the provider's own id (e.g.
`deepseek-chat`) — the `claude-*` model regex relaxes when a provider
is declared. The runtime injects `ANTHROPIC_BASE_URL`, the API key (via
sac's `SAC_ANTHROPIC_API_KEY` handoff), and a clean per-agent
`CLAUDE_CONFIG_DIR` into the container, and fails loud if
`$<auth_token_env>` is unset on the host.

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
