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
  runtime: apptainer                     # optional; only `apptainer` accepted; empty defaults to `apptainer` (since 2026-05-13)
  workdir: ~/proj                        # mounted rw at /work
  to_home: ./to_home                     # mirrored into the agent $HOME at start (auto-discovers ./to_home; default "./to_home")
  python-venv: auto                      # string or list — fallback chain
  env-file: .env                         # string or list of dotenv paths   (VERIFY: validator currently rejects — must be added to _KNOWN_SPEC_KEYS)
  multiplexer: tmux                      # tmux | screen                    (VERIFY: validator currently rejects — must be added to _KNOWN_SPEC_KEYS)

  apptainer:    { ... }
  claude:       { ... }
  mcp_servers:  { ... }
  health:       { ... }
  restart:      { ... }
  autonomous:   { ... }
  a2a:          { host: 127.0.0.1, port: auto }    # port: auto | <int> | null (disable)
  proxy:        { upstream: https://peer/, trust: untrusted }   # kind: AgentProxy only
  listen:                                # LIST of side-port DECLARATIONS (no binding):
    - { port: 9000, proto: tcp, name: api, owner: app }
    - { proto: unix, path: /tmp/x.sock, name: ipc }
  # NOTE: the host-level `sac listen` server port lives in
  # ~/.scitex/agent-container/config.yaml (listen.port, default 7878),
  # NOT in agent spec.yaml.
  startup:                               # (optional) ready-pattern gating block (todo#291)
    commands: [...]                      # shadows top-level startup_commands when set
    ready_patterns: [...]                # regex strings (or { regex: "..." } dicts)
    ready_idle_ticks: 3
    ready_poll_interval_seconds: 0.5
    ready_timeout_seconds: 60
    on_timeout: capture_and_proceed      # capture_and_proceed | capture_and_fail
  context_management:                    # context auto-management (compact/restart/noop)
    trigger_at_percent: 70
    strategy: noop                       # compact | restart | noop
    warn_before_n_checks: 0
    check_interval_seconds: 300
  telegram:     { bot_token_env: ..., allowed_users: [...], auto_connect: true, greeting: ... }
  hooks:        { pre_start: [...], post_start: [...], pre_stop: [...], post_stop: [...] }
  extensions:   { ... }                  # opaque per-deployment dict

  startup_commands:                      # SHELL before claude starts (list of {delay, command} dicts)
    - { delay: 0, command: "echo hi" }
  startup_prompts:  [...]                # TEXT fed to claude as first user msg
  session: continue                      # top-level shortcut overriding spec.claude.session

  host:  gpu-box                         # mutually exclusive: singleton on one peer
  hosts: [laptop, gpu-box, nas]          # OR multi-instance, one per peer
```

## Field reference

### `metadata.labels` → AgentCard fields

> **Note on naming — two "skills" concepts.** A2A's AgentCard has a
> standard top-level `skills[]` array used to *advertise* capabilities
> to peers (id / name / description / tags / examples). Anthropic's
> Claude Code separately uses "skills" for *prompt-fragment* markdown
> files under `<HOME>/.claude/skills/<name>/` that the SDK loads into
> the agent's own context. Both share the English word but live at
> orthogonal layers:
>
> | Layer | Drives | Effect |
> |---|---|---|
> | `metadata.labels.skills` (CSV) | A2A `skills[0].tags` + `x-scitex-agent-container.required_skills` | Advertises capabilities on the card; **no behaviour change inside the agent** |
> | `spec.to_home/.claude/skills/<name>/SKILL.md` (files) | Materialised at `runtime/<name>/home/.claude/skills/` (ADR-0006) and surfaced via `spec.skills.required[]` `@`-imports in the auto-generated CLAUDE.md | Loaded into the agent's prompt by the Claude SDK |
>
> Also note A2A's separate top-level `capabilities` field is for
> *transport* properties (`streaming`, `pushNotifications`, etc.) —
> not a synonym for "what the agent can do". The "can do" surface is
> always `skills[]`.

The AgentCard at `GET /.well-known/agent-card.json` (per-agent sidecar
when `spec.a2a.port` is set) and `GET /agents/<name>/card`
(host-level `sac listen`) is built **entirely** from spec.yaml:

| AgentCard field                       | spec.yaml source                                 |
|---------------------------------------|--------------------------------------------------|
| `name`                                | parent directory of `spec.yaml`                  |
| `description`                         | `metadata.labels.description` (else auto)        |
| `version`                             | `apiVersion`                                     |
| `url`                                 | `<base>/agents/<name>`                        |
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
| `runtime`            | `apptainer` (optional)     | Empty/unset defaults to `apptainer`; any other value is rejected. docker/podman were dropped 2026-05-13 |
| `workdir`            | path                       | Mounted rw at `/work` (default: `~/.scitex/agent-container/runtime/agents/<name>/`) |
| `to_home`            | path                       | Mirrored into the agent's container `$HOME` (= `runtime/<name>/home/`) at start. Every path under `to_home/` lands at the same relative path under `$HOME`. Default `./to_home` — auto-discovers a sibling `to_home/` next to `spec.yaml`. |
| `python-venv`        | string \| list             | Pre-activated for startup_commands; `auto` probes `~/.venv-3.11`, `~/.venv` |
| `env-file`           | string \| list             | dotenv paths sourced at start. **(VERIFY: parsed by the loader but currently rejected by `_validation._KNOWN_SPEC_KEYS` — known parser/validator drift.)** |
| `user`               | `""` \| `"host"` \| `"<uid>:<gid>"` | Container user override; empty = image default. |
| `multiplexer`        | `tmux` \| `screen`         | Long-lived session host (default `tmux`). **(VERIFY: parsed by the loader but currently rejected by `_validation._KNOWN_SPEC_KEYS` — known parser/validator drift.)** |
| `host` / `hosts`     | string / list of strings   | Singleton on one peer / multi-instance one-per-peer (mutually exclusive). `hosts: "all"` = every fleet host. |
| `session`            | string                     | Top-level shortcut overriding `spec.claude.session`; legacy aliases accepted (`continue-or-new`, `new`). |
| `screen.name`        | string                     | Legacy metadata (agent display name in `sac fleet`). Default = agent name. Does NOT drive a multiplexer. |
| `startup_commands[]` | list of `{delay, command}` | Run **before** Claude starts. Each item is a dict with optional `delay` (int seconds, default 0) and required `command` (string); bare strings are not accepted. |
| `startup_prompts[]`  | list of strings            | Fed to Claude as first user message(s)                                   |

### `spec.apptainer` — engine knobs

| Field         | Type                          | Description                                                |
|---------------|-------------------------------|------------------------------------------------------------|
| `image`       | path to `.sif`                | `sac-scitex.sif` (full stack) or `sac-base.sif` (minimal). Optional; empty falls back to the sac default SIF at dispatch. |
| `overlay`     | path                          | Writable rw layer above the SIF                            |
| `overlay_size` | size string (e.g. `"5G"`, `"500M"`) | When set together with `overlay`, sac auto-creates the overlay image at that path with the given size if it doesn't exist (declarative — no manual `apptainer overlay create` step). Units: M/MB/G/GB only (K/KB rejected). Empty = no auto-create (missing overlay raises a clear FileNotFoundError at launch). |
| `overlay_create_if_missing` | bool (default `true`) | Gate for the auto-create behaviour above. When `false` AND the overlay is missing, sac raises FileNotFoundError without attempting creation (operator must pre-create with `apptainer overlay create`). |
| `binds[]`     | `host:container[:ro\|rw]` (or legacy `{src,dst,mode}` dict) | Bind mounts. Source side supports `~` / `$VAR` (sac expands before calling apptainer). Destination MUST be absolute (apptainer rejects relative / `~` / `$VAR`); conventional roots are `/home/agent/...` (D5 canonical HOME), `/srv/`, `/work/`, `/opt/`, `/data/`. The legacy `{src, dst, mode}` dict form is still accepted by the parser and normalized to the string form. |
| `env`         | key-value dict                | Env vars exported into the container                       |
| `container_workdir` | path (default `/work`)  | Working directory inside the container.                    |
| `nv` / `rocm` | bool                          | Forward host NVIDIA / AMD ROCm libs. (DESIGN — mutual exclusion not currently enforced by the parser.) |
| `raw_args[]`  | list of strings               | **Escape hatch** — appended verbatim to `apptainer exec`   |
| `post` / `environment` / `def_file` | string / KV dict / path | Apptainer `%post` shell snippet, `%environment` KV map, and override `.def` path for `apptainer build`. Empty / missing → no build extension. |
| `relaxed`     | bool (default `false`)        | **(DESIGN — not yet implemented in the parser.)** Intent: opt OUT of hardened-by-default isolation. When `false` (default), sac auto-prepends `--containall` / `--cleanenv` / `--writable-tmpfs` / `--home /home/agent`. Set `true` to disable; see [`docs/isolation.md`](isolation.md) + [`docs/adr/0001-isolation-hardening.md`](adr/0001-isolation-hardening.md). TODO: wire into `ApptainerSpec`. |
| `fakeroot`    | bool (default `false`)        | **(DESIGN — not yet implemented in the parser.)** Intent: apptainer `--fakeroot` — uid 0 inside via user-namespace remap; host uid unchanged. D5 preflight detects userns-fakeroot via `/proc/self/uid_map` and accepts uid 0 only when remapped. TODO: wire into `ApptainerSpec`. |

### `spec.claude` — SDK knobs

| Field                       | Type                                  | Description                                                       |
|-----------------------------|---------------------------------------|-------------------------------------------------------------------|
| `model`                     | `haiku` \| `sonnet` \| `opus` \| ...  | Claude model                                                      |
| `session`                   | `continue` \| `new-session` \| `resume`| Session strategy (default `continue` — safe fallback). Legacy aliases `continue-or-new`, `new` accepted |
| `resume_id`                 | string                                | Explicit session UUID for `session: resume`                       |
| `continue_max_age_minutes`  | int                                   | Only resume if session.jsonl is newer than N minutes              |
| `flags[]`                   | list of strings                       | Extra flags appended to `claude` invocation                       |
| `channels[]`                | `server:<name>` / `plugin:<id>@<v>`   | MCP push channels (passed as `claude --channels`)                 |
| `auto_accept`               | bool (default `True`)                 | Auto-confirm permission prompts in the TUI                        |
| `raw_options`               | dict                                  | **Escape hatch** — splatted into `ClaudeAgentOptions(**raw_options)` |

### `spec.health` / `spec.restart` / `spec.watchdog` / `spec.autonomous`

| Field                       | Description                                                                              |
|-----------------------------|------------------------------------------------------------------------------------------|
| `health.enabled`            | bool — enable periodic liveness probe                                                    |
| `health.interval`           | seconds between probes                                                                   |
| `health.timeout`            | per-probe timeout                                                                        |
| `health.method`             | `sdk-alive` (only value accepted by the validator). NOTE: the parser default is the legacy string `multiplexer-alive`; with the validator pin in place, any explicit value other than `sdk-alive` is rejected at load time. |
| `autonomous.idle_kick_after_s` | int seconds — nudge cadence when no tool activity (default 120)                       |
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
| `a2a.host`   | Bind interface for the per-agent A2A sidecar (default `127.0.0.1`).                  |
| `a2a.port`   | `auto` (default) — sac claims a free port from `~/.scitex/agent-container/config.yaml`'s `a2a.port_range` (default 19000-19999), persists in `state.db`, surfaces via `sac agents list`. Set an explicit int (e.g. `7901`) to pin for a stable external URL. Set `null` to disable the sidecar entirely. **Most operators never touch this** — auto is the right default. |
| `listen[]`   | LIST of side-port DECLARATIONS (NOT a single port override). Each item: `{port, proto, path, name, owner}`. `proto`: `tcp` (default) / `udp` / `unix`. Entries that fail validation (`tcp`/`udp` needs `port>0`; `unix` needs `path`) are silently dropped. **The container does NOT bind these — declarations only**, surfaced on the AgentCard for peers. The host-level `sac listen` server port (default 7878) is configured in `~/.scitex/agent-container/config.yaml` under `listen.port`, NOT here. |

The per-agent sidecar binds the **same URL shape** as `sac listen`
(`/agents/<name>/{turn,send,card}`, `/v1/a2a/agents/<name>/...`,
`/.well-known/agent-card.json`, `/health`), so the same client code
works against either transport. Per-agent ports are an internal IPC
mechanism between `sac listen` and the runner (different processes);
clients reach every agent through the **one stable host port** at
`sac listen` (default `:7878`).

The AgentCard's `url` field advertises the **sac listen** URL
(`http://127.0.0.1:7878/agents/<name>`) regardless of which
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
`to_home/.claude/skills/` (a sibling directory next to `spec.yaml`,
materialized into the agent's `$HOME` at start).

For AgentCard publication, declare the skill IDs via
`metadata.labels.skills` as a CSV (e.g. `skills: "scitex-dev, gh-cli, git"`).
The list ends up in the card's `skills[0].tags` (unioned with
`metadata.labels.capabilities`) and `x-scitex-agent-container.required_skills`.

### `spec.mcp_servers`

A dict-of-dicts merged into `<workdir>/.mcp.json` at start. Mirrors
the `.mcp.json` shape directly. Use this OR drop a `.mcp.json` into
`to_home/` (lands at `$HOME/.mcp.json`).

### `spec.telegram` / `spec.hooks` / `spec.extensions`

| Field                  | Description                                                                |
|------------------------|----------------------------------------------------------------------------|
| `telegram.bot_token_env`| Env var name holding the bot token (default `SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN`) |
| `telegram.allowed_users`| List of Telegram user IDs (strings) allowed to talk to this bridge        |
| `telegram.auto_connect`| bool (default `true`) — auto-attach the bridge at agent start              |
| `telegram.greeting`    | Optional greeting string posted on connect                                 |
| `hooks.pre_start[]`    | Shell commands before `apptainer exec` (a `mkdir -p <workdir>/.claude` is auto-prepended) |
| `hooks.post_start[]`   | Shell commands after the runner reports ready                              |
| `hooks.pre_stop[]`     | Shell commands before SIGTERM                                              |
| `hooks.post_stop[]`    | Shell commands after the runner exits                                      |
| `extensions`           | Opaque dict — read by downstream tooling (priority, owner, etc.)           |

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

- [`full-agent/`](../examples/agents/full-agent/) — annotated spec exercising every supported field (plus `to_home/` layout)
- [`minimal-agent/`](../examples/agents/minimal-agent/) — bare minimum, no `to_home`
- [`hello-agent/`](../examples/agents/hello-agent/) — quickstart with `startup_prompts`
- [`proxy-agent/`](../examples/agents/proxy-agent/) — `kind: AgentProxy` forwarder example
