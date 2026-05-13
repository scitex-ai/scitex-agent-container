# YAML Spec Reference (v3)

Container + session knobs nest under the engine that interprets them
(`spec.apptainer.*`, `spec.claude.*`). Cross-cutting knobs (workdir,
a2a, health, restart, autonomous, listen, skills, telegram, hooks)
stay at the top level. Every curated block has a `raw_*` escape hatch
— the full underlying surface is always reachable.

The agent name is the parent directory of `spec.yaml` (dir-as-SSoT —
no `metadata.name` field).

## Quick links

- Annotated full example: [`examples/agents/full-agent/spec.yaml`](../examples/agents/full-agent/spec.yaml) — every supported field with inline comments
- Minimal example: [`examples/agents/minimal-agent/spec.yaml`](../examples/agents/minimal-agent/spec.yaml)
- Quickstart with `startup_prompts`: [`examples/agents/hello-agent/spec.yaml`](../examples/agents/hello-agent/spec.yaml)

## Top-level shape

```yaml
apiVersion: scitex-agent-container/v3    # REQUIRED — v1/v2 raise loud validation errors
kind: Agent                              # REQUIRED — Agent | AgentProxy
                                         # (AgentProxy → HTTP forwarder, no SDK;
                                         #  see spec.proxy + examples/agents/proxy-agent)

metadata:
  labels:                                # drives `sac fleet` filters AND the AgentCard
    role: ecosystem-auditor
    team: lab-a
    description: ...                     # → AgentCard.description
    function: audit, git status, ...     # → AgentCard.skills[0].description
    capabilities: audit,health-check     # CSV → AgentCard.skills[0].tags
    cardinality: singleton               # → AgentCard.x-scitex-agent-container.cardinality

spec:
  runtime: apptainer                     # REQUIRED — only value accepted since 2026-05-13
  workdir: ~/proj                        # mounted rw at /work
  dot_claude: ./dot_claude               # merged into <workdir>/.claude/ at start
  python-venv: auto                      # string or list — fallback chain
  env-file: .env                         # string or list of dotenv paths
  multiplexer: tmux                      # tmux | screen

  apptainer:    { ... }
  claude:       { ... }
  mcp_servers:  { ... }
  health:       { ... }
  restart:      { ... }
  autonomous:   { ... }
  a2a:          { port: 7901 }
  proxy:        { upstream: https://peer/, trust: untrusted }   # kind: AgentProxy only
  listen:       { port: 7878 }
  skills:       { required: [...] }
  telegram:     { ... }
  hooks:        { pre_start: [...], post_start: [...], pre_stop: [...] }
  extensions:   { ... }                  # opaque per-deployment dict

  startup_commands: [...]                # SHELL before claude starts
  startup_prompts:  [...]                # TEXT fed to claude as first user msg

  host:  gpu-box                         # mutually exclusive: singleton on one peer
  hosts: [laptop, gpu-box, nas]          # OR multi-instance, one per peer
```

## Field reference

### `metadata.labels` → AgentCard fields

The AgentCard at `GET /.well-known/agent-card.json` (per-agent sidecar
when `spec.a2a.port` is set) and `GET /v1/sac/agents/<name>/card`
(host-level `sac listen`) is built **entirely** from spec.yaml:

| AgentCard field                       | spec.yaml source                                 |
|---------------------------------------|--------------------------------------------------|
| `name`                                | parent directory of `spec.yaml`                  |
| `description`                         | `metadata.labels.description` (else auto)        |
| `version`                             | `apiVersion`                                     |
| `url`                                 | `<base>/v1/sac/agents/<name>`                        |
| `provider.organization`               | `metadata.labels.team`                           |
| `skills[0].id` / `name`               | `metadata.labels.role`                           |
| `skills[0].description`               | `metadata.labels.function`                       |
| `skills[0].tags`                      | `metadata.labels.capabilities` ∪ `metadata.labels.skills` (both CSV) |
| `x-scitex-agent-container.role_class` | `metadata.labels.role`                           |
| `x-scitex-agent-container.cardinality`| `metadata.labels.cardinality`                    |
| `x-scitex-agent-container.scheduling` | derived from `spec.host` / `spec.hosts`          |
| `x-scitex-agent-container.runtime`    | `spec.runtime`                                   |
| `x-scitex-agent-container.model`      | `spec.claude.model` (v3) / `spec.model` (v2 back-compat) |
| `x-scitex-agent-container.required_skills` | `metadata.labels.skills` (CSV) ∪ legacy `spec.skills.required` |
| `x-scitex-agent-container.multiplexer`| `spec.multiplexer`                               |

### `spec` — top-level

| Field                | Type                       | Description                                                              |
|----------------------|----------------------------|--------------------------------------------------------------------------|
| `runtime`            | `apptainer` (REQUIRED)     | Only value accepted; docker/podman were dropped 2026-05-13               |
| `workdir`            | path                       | Mounted rw at `/work` (default: `~/.scitex/agent-container/runtime/agents/<name>/`) |
| `dot_claude`         | path                       | Materialized into `<workdir>/.claude/` (default: auto-discover sibling)  |
| `python-venv`        | string \| list             | Pre-activated for startup_commands; `auto` probes `~/.venv-3.11`, `~/.venv` |
| `env-file`           | string \| list             | dotenv paths sourced at start                                            |
| `user`               | string                     | Container user override                                                  |
| `multiplexer`        | `tmux` \| `screen`         | Long-lived session host                                                  |
| `host` / `hosts`     | string / list of strings   | Singleton on one peer / multi-instance one-per-peer (mutually exclusive) |
| `startup_commands[]` | list of shell commands     | Run **before** Claude starts                                             |
| `startup_prompts[]`  | list of strings            | Fed to Claude as first user message(s)                                   |

### `spec.apptainer` — engine knobs

| Field         | Type                          | Description                                                |
|---------------|-------------------------------|------------------------------------------------------------|
| `image`       | path to `.sif` (REQUIRED)     | `sac-scitex.sif` (full stack) or `sac-base.sif` (minimal)  |
| `overlay`     | path                          | Writable rw layer above the SIF                            |
| `binds[]`     | `host:container[:ro\|rw]`     | Bind mounts (default: apptainer auto-binds `/home/$USER`)  |
| `env`         | key-value dict                | Env vars exported into the container                       |
| `nv` / `rocm` | bool                          | Forward host NVIDIA / AMD ROCm libs (mutually exclusive)   |
| `raw_args[]`  | list of strings               | **Escape hatch** — appended verbatim to `apptainer exec`   |

### `spec.claude` — SDK knobs

| Field                       | Type                                  | Description                                                       |
|-----------------------------|---------------------------------------|-------------------------------------------------------------------|
| `model`                     | `haiku` \| `sonnet` \| `opus` \| ...  | Claude model                                                      |
| `session`                   | `continue` \| `new-session` \| `resume`| Session strategy (default `continue` — safe fallback). Legacy aliases `continue-or-new`, `new` accepted |
| `resume_id`                 | string                                | Explicit session UUID for `session: resume`                       |
| `continue_max_age_minutes`  | int                                   | Only resume if session.jsonl is newer than N minutes              |
| `flags[]`                   | list of strings                       | Extra flags appended to `claude` invocation                       |
| `channels[]`                | `server:<name>` / `plugin:<id>@<v>`   | MCP push channels (passed as `claude --channels`)                 |
| `auto_accept`               | bool                                  | Auto-confirm permission prompts in the TUI                        |
| `raw_options`               | dict                                  | **Escape hatch** — splatted into `ClaudeAgentOptions(**raw_options)` |

### `spec.health` / `spec.restart` / `spec.watchdog` / `spec.autonomous`

| Field                       | Description                                                                              |
|-----------------------------|------------------------------------------------------------------------------------------|
| `health.enabled`            | bool — enable periodic liveness probe                                                    |
| `health.interval`           | seconds between probes                                                                   |
| `health.timeout`            | per-probe timeout                                                                        |
| `health.method`             | `sdk-alive` (only currently supported)                                                   |
| `restart.policy`            | `never` \| `on-failure` \| `always`                                                      |
| `restart.max_retries`       | int                                                                                      |
| `restart.backoff.initial`   | seconds before first retry                                                               |
| `restart.backoff.max`       | cap on backoff                                                                           |
| `restart.backoff.multiplier`| exponential factor                                                                       |
| `watchdog.enabled`          | parsed for back-compat; lifecycle managed via hooks                                      |
| `autonomous.enabled`        | drive turns until `drive_until` token or `max_turns`                                     |
| `autonomous.drive_until`    | string token Claude prints when done (default `DONE`)                                    |
| `autonomous.max_turns`      | int                                                                                      |
| `autonomous.kick_text`      | nudge sent when Claude pauses                                                            |

### `spec.a2a` / `spec.listen` — network endpoints

| Field        | Description                                                                          |
|--------------|--------------------------------------------------------------------------------------|
| `a2a.port`   | `auto` (default) — sac claims a free port from `~/.scitex/agent-container/config.yaml`'s `a2a.port_range` (default 19000-19999), persists in `state.db`, surfaces via `sac agents list`. Set an explicit int (e.g. `7901`) to pin for a stable external URL. Set `null` to disable the sidecar entirely. **Most operators never touch this** — auto is the right default. |
| `listen.port`| Override for the host-level `sac listen` server port (default 7878)                  |

The per-agent sidecar binds the **same URL shape** as `sac listen`
(`/v1/sac/agents/<name>/{turn,send,card}`, `/v1/a2a/agents/<name>/...`,
`/.well-known/agent-card.json`, `/health`), so the same client code
works against either transport. Per-agent ports are an internal IPC
mechanism between `sac listen` and the runner (different processes);
clients reach every agent through the **one stable host port** at
`sac listen` (default `:7878`).

The AgentCard's `url` field advertises the **sac listen** URL
(`http://127.0.0.1:7878/v1/sac/agents/<name>`) regardless of which
endpoint served the card, so external A2A clients caching the card
get a URL that survives per-agent port churn.

### `~/.scitex/agent-container/config.yaml`

Host-wide sac configuration. All keys optional; defaults shown.

```yaml
listen:
  host: 127.0.0.1        # bind interface for sac listen (loopback only)
  port: 7878             # host control-plane port

a2a:
  port_range: [19000, 19999]   # range the auto-allocator picks from
```

### Skills

`spec.skills` was **removed in v3** — skills now live under
`dot_claude/skills/` (a sibling directory next to `spec.yaml`,
materialized into the workspace at start).

For AgentCard publication, declare the skill IDs via
`metadata.labels.skills` as a CSV (e.g. `skills: "scitex-dev, gh-cli, git"`).
The list ends up in the card's `skills[0].tags` (unioned with
`metadata.labels.capabilities`) and `x-scitex-agent-container.required_skills`.

### `spec.mcp_servers`

A dict-of-dicts merged into `<workdir>/.mcp.json` at start. Mirrors
the `.mcp.json` shape directly. Use this OR drop a `.mcp.json` into
`dot_claude/` — both are merged.

### `spec.telegram` / `spec.hooks` / `spec.extensions`

| Field                | Description                                                                  |
|----------------------|------------------------------------------------------------------------------|
| `telegram.enabled`   | bool — enable alerting bridge (consumed by claude-code-telegrammer)          |
| `telegram.chat_id`   | Telegram chat ID                                                             |
| `hooks.pre_start[]`  | Shell commands before `apptainer exec` (a `mkdir -p <workdir>/.claude` is auto-prepended) |
| `hooks.post_start[]` | Shell commands after the runner reports ready                                |
| `hooks.pre_stop[]`   | Shell commands before SIGTERM                                                |
| `extensions`         | Opaque dict — read by downstream tooling (priority, owner, etc.)             |

## Lifetime / session selection

Default = long-lived + safe-fallback session continue. The `sac
agents start` CLI overrides at start time:

```bash
sac agents start <name> --one-shot                 # exits after first startup_prompt
sac agents start <name> --session continue         # default (try continue, fall back to fresh)
sac agents start <name> --session new-session      # force fresh
sac agents start <name> --resume <sid>             # implies --session resume
```

CLI flags ALWAYS override the YAML — one-direction precedence so a
per-invocation tweak doesn't mutate the persistent default.

## `kind: AgentProxy` — HTTP forwarder agents

A proxy agent forwards `POST /v1/turn` to an **external A2A
endpoint** instead of running a Claude SDK conversation in-process.
There is no SDK in the container; the runner is a thin Starlette
forwarder (image: `sac-proxy.sif`, lighter than `sac-scitex.sif` —
no Python ML stack).

Authoring contract:

- `kind: AgentProxy` (instead of `kind: Agent`).
- `spec.proxy` is **REQUIRED**.
- `spec.claude`, `spec.startup_prompts`, `spec.startup_commands` are
  rejected at validation time (no SDK to configure / prompt).
- `spec.a2a.port` works the same — that's the port operators POST to.

### `spec.proxy` reference

| Field           | Type              | Default       | Notes                                                                                  |
|-----------------|-------------------|---------------|----------------------------------------------------------------------------------------|
| `upstream`      | string (REQUIRED) | —             | Full URL to the upstream A2A endpoint (must start with `http://` or `https://`).        |
| `trust`         | enum              | `untrusted`   | `untrusted` / `local-mesh` / `trusted`. Advisory — surfaced on our AgentCard.           |
| `redact`        | list[str]         | `[]`          | Substring tokens; any inbound `text` containing one is refused HTTP 400 before forward. |
| `timeout_s`     | float > 0         | `30.0`        | Per-turn upstream HTTP timeout. Longer forwards return HTTP 504 to the caller.          |

### Security notes

- Proxy is HTTP-only — no mTLS in the MVP (the `trusted` level is
  reserved for future work).
- Default trust is `untrusted`; operators must opt in to anything
  more permissive.
- Egress lockdown is application-layer: a 3xx redirect from upstream
  to a *different* host is rejected with HTTP 502. The MVP does
  not enforce an apptainer `--net` policy.
- Runs in `sac-proxy.sif` — see `containers/sac-proxy.def`.

See [`examples/agents/proxy-agent/spec.yaml`](../examples/agents/proxy-agent/spec.yaml)
for a complete minimal example.

## Examples

Copy from [`examples/agents/`](../examples/agents/):

- [`full-agent/`](../examples/agents/full-agent/) — annotated spec exercising every supported field (plus `dot_claude/` layout)
- [`minimal-agent/`](../examples/agents/minimal-agent/) — bare minimum, no `dot_claude`
- [`hello-agent/`](../examples/agents/hello-agent/) — quickstart with `startup_prompts`
- [`proxy-agent/`](../examples/agents/proxy-agent/) — `kind: AgentProxy` forwarder example
