# YAML Spec Reference (v3)

Container + session knobs nest under the engine that interprets them
(`spec.apptainer.*`, `spec.claude.*`). Cross-cutting knobs (workdir,
a2a, health, restart, autonomous, listen, skills, telegram, hooks)
stay at the top level. Every curated block has a `raw_*` escape hatch
— the full underlying surface is always reachable.

> **On the name `spec.claude`.** It is a legacy key name, not a scope
> claim: the block carries the SDK-harness session/model knobs
> generally, and its `provider` sub-key demonstrably configures
> non-Anthropic endpoints. Do not read `spec.claude.*` as "the Claude
> harness only" — and do not confuse `spec.claude.provider` (which
> *inference endpoint* answers) with the top-level `spec.harness`
> (which *agent program* drives the turn). The two axes compose.

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
| `harness`            | `anthropic` (default) \| `openai` \| `codex` | **Which agent SDK runs the session** (NOT the same field as `spec.claude.provider`, which points the *Claude* SDK at an Anthropic-compatible inference gateway — the two axes compose). `openai` runs the agent on the `openai-agents` SDK: at launch sac injects `SAC_OPENAI_API_KEY` + `OPENAI_API_KEY` (resolved host-side, shell export > `$HOME/.env`, `SAC_OPENAI_API_KEY` preferred over `OPENAI_API_KEY`) and forwards `OPENAI_BASE_URL` / `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID` / `SAC_OPENAI_MODEL` when set on the host; NO Anthropic OAuth env or credentials bind is emitted. Fail-loud when no key resolves or when composed with an active `spec.claude.provider` override. **Launch caveat today — read this before choosing a harness:** the registry has four entries (`claude-code-tui`, `claude-agent-sdk`, `openai-agents`, `codex-sdk`) and `spec.harness` accepts three values, but **only `anthropic` can be STARTED**. `openai` and `codex` both load, validate and resolve to their registry entries, and then every lifecycle launch path REFUSES them — loudly, rather than silently launching a Claude runner under a spec that asked for something else. A2A serving uses the `openai_session` executor (`spec.a2a.handler`), which is the working path for the OpenAI SDK meanwhile; there is no equivalent executor for `codex` yet. **`codex` is also a legal `spec.claude.provider` value and means something different there** — as a HARNESS the Codex agent program runs the loop; as a PROVIDER Claude Code still drives and Codex only answers. **Ops-only override:** exporting `SAC_PROVIDER=openai` (or `anthropic`) in the shell that runs `sac agents start` overrides `spec.harness` for every launch from that shell — an operations escape hatch for emergency flips / A/B smoke tests, never a spec surface; unknown values are rejected loudly. (The env var keeps its older `SAC_PROVIDER` name.) |
| `provider`           | *(deprecated alias of `harness`)* | The spelling this field had before it was renamed. **Still honoured** — a spec carrying `provider:` loads unchanged and satisfies the explicit-fields requirement for `harness`; starting such an agent logs a one-line deprecation naming the agent. Writing BOTH keys is fine when they carry the same value; writing both with DIFFERENT values is a hard load error naming both, because a spec that says two things does not say which harness it wants. It was renamed because it never named a provider: it selects which agent PROGRAM drives the loop, while `spec.claude.provider` (unchanged, and still correctly named) selects which inference endpoint answers. |
| `engines`            | mapping `<key>: {harness, model, provider, default, reasoning_effort, max_context_tokens, env}` | **SEVERAL declared backends, ONE picked at start.** Optional — a spec that omits it declares its single backend the old way (`harness` + `spec.claude.model` + `spec.claude.provider`) and is unchanged. See **`spec.engines`** below and ADR-0024. |
| `residency`          | `resident` (default) \| `one-shot` | **Does the daemon outlive its work?** (v4 residency axis.) `resident` — the fleet posture — keeps the session daemon alive after a conversation completes, parked awaiting more turns; a turn driver that returns on its own is then a residency VIOLATION (ExitRecord `harness-returned`/`crashed`, non-zero exit). `one-shot` makes a normal completion the PLAN: the mission turn carries `exit_after`, and the daemon exits `0` with ExitRecord reason `oneshot-complete` — for experiment trials and one-off workers. Absence/null defaults to `resident` (the axis postdates the live corpus; the v3→v4 converter materializes the explicit line — requiring it is that later step). Illegal values are rejected loudly naming the closed set. Only runner-hosted harnesses honour it: `one-shot` on the interactive TUI (`runtime: tui`, which has no session daemon) or on `kind: AgentProxy` is refused at validation time. |
| `access`             | `full` (default) \| `capsule` | Host-access posture. `full` (the default; absent → `full`) binds the operator's WHOLE home rw at its canonical path (`/home/<user>:/home/<user>:rw`) so the agent reaches every project + config, and opens `--pwd` at the workdir's **canonical** path (the `/work` alias stays bound for back-compat). The agent's own `$HOME=/home/agent` (credentials / to_home / overlay wiring) is untouched. `capsule` restricts the agent to ONLY the binds explicitly listed in the spec + the `/work` alias (pre-2026-06-19 behaviour) — for leak-prevention agents. |
| `workdir`            | path                       | Mounted rw at the canonical host path **and** the `/work` alias under `access: full`; at `/work` only under `access: capsule` (default: `~/.scitex/agent-container/runtime/agents/<name>/`). `--pwd` is the canonical path (full) or `/work` (capsule). |
| `to_home`            | path                       | Mirrored into the agent's container `$HOME` (= `runtime/<name>/home/`) at start. Every path under `to_home/` lands at the same relative path under `$HOME`. Default `./to_home` — auto-discovers a sibling `to_home/` next to `spec.yaml`. |
| `python-venv`        | string \| list             | Pre-activated for startup_commands; `auto` probes `~/.venv-3.11`, `~/.venv` |
| `env-file`           | string \| list             | dotenv paths sourced at start. **(VERIFY: parsed by the loader but currently rejected by `_validation._KNOWN_SPEC_KEYS` — known parser/validator drift.)** |
| `user`               | `""` \| `"host"` \| `"<uid>:<gid>"` | Container user override; empty = image default. |
| `multiplexer`        | `tmux` \| `screen`         | Long-lived session host (default `tmux`). **(VERIFY: parsed by the loader but currently rejected by `_validation._KNOWN_SPEC_KEYS` — known parser/validator drift.)** |
| `host` / `hosts`     | string / list of strings   | Singleton on one peer / multi-instance one-per-peer (mutually exclusive). `hosts: "all"` = every fleet host. |
| `session`            | string                     | Top-level shortcut overriding `spec.claude.session`; legacy aliases accepted (`continue-or-new`, `new`). |
| `screen.name`        | string                     | Legacy metadata (agent display name in `sac fleet`). Default = agent name. Does NOT drive a multiplexer. |
| `startup_commands[]` | list of `{delay, command}` | Run **before** the harness process starts. Each item is a dict with optional `delay` (int seconds, default 0) and required `command` (string); bare strings are not accepted. |
| `startup_prompts[]`  | list of strings            | Fed to the agent as first user message(s)                                |

### `spec.apptainer` — engine knobs

| Field         | Type                          | Description                                                |
|---------------|-------------------------------|------------------------------------------------------------|
| `image`       | path to `.sif`                | `sac-scitex.sif` (full stack) or `sac-base.sif` (minimal). Optional; empty falls back to the sac default SIF at dispatch. |
| `overlay`     | path                          | Writable rw layer above the SIF                            |
| `overlay_size` | size string (e.g. `"5G"`, `"500M"`) | When set together with `overlay`, sac auto-creates the overlay image at that path with the given size if it doesn't exist (declarative — no manual `apptainer overlay create` step). Units: M/MB/G/GB only (K/KB rejected). Empty = no auto-create (missing overlay raises a clear FileNotFoundError at launch). |
| `overlay_create_if_missing` | bool (default `true`) | Gate for the auto-create behaviour above. When `false` AND the overlay is missing, sac raises FileNotFoundError without attempting creation (operator must pre-create with `apptainer overlay create`). |
| `tmpfs_size`  | size string (default `"2G"`)  | Minimum free-space guarantee for the container's `/tmp` (and `/var/tmp`). A `--containall` container otherwise gets a 64 MB session tmpfs at `/tmp` that fills mid-run during the full test suite. sac emits `--workdir <state_dir>/tmp-scratch` to relocate `/tmp` onto the host filesystem (capacity >> 64 MB) and fails loud (`TmpfsSpaceError`) if that filesystem has less than `tmpfs_size` free. Units: M/MB/G/GB only (K/KB rejected). NOT a hard cap (unprivileged apptainer can't size-cap a tmpfs). Set to `""` to opt out (legacy 64 MB tmpfs). Skipped when the operator declares their own `--workdir`/`-W` in `raw_args`. |
| `binds[]`     | `host:container[:ro\|rw]` (or legacy `{src,dst,mode}` dict) | Bind mounts. Source side supports `~` / `$VAR` (sac expands before calling apptainer). Destination MUST be absolute (apptainer rejects relative / `~` / `$VAR`); conventional roots are `/home/agent/...` (D5 canonical HOME), `/srv/`, `/work/`, `/opt/`, `/data/`. The legacy `{src, dst, mode}` dict form is still accepted by the parser and normalized to the string form. |
| `env`         | key-value dict                | Env vars exported into the container                       |
| `container_workdir` | path (default `/work`)  | Working directory inside the container.                    |
| `nv` / `rocm` | bool                          | Forward host NVIDIA / AMD ROCm libs. (DESIGN — mutual exclusion not currently enforced by the parser.) |
| `raw_args[]`  | list of strings               | **Escape hatch** — appended verbatim to `apptainer exec`   |
| `post` / `environment` / `def_file` | string / KV dict / path | Apptainer `%post` shell snippet, `%environment` KV map, and override `.def` path for `apptainer build`. Empty / missing → no build extension. |
| `relaxed`     | bool (default `false`)        | **(DESIGN — not yet implemented in the parser.)** Intent: opt OUT of hardened-by-default isolation. When `false` (default), sac auto-prepends `--containall` / `--cleanenv` / `--writable-tmpfs` / `--home /home/agent`. Set `true` to disable; see [`docs/isolation.md`](isolation.md) + [`docs/adr/0001-isolation-hardening.md`](adr/0001-isolation-hardening.md). TODO: wire into `ApptainerSpec`. |
| `fakeroot`    | bool (default `false`)        | **(DESIGN — not yet implemented in the parser.)** Intent: apptainer `--fakeroot` — uid 0 inside via user-namespace remap; host uid unchanged. D5 preflight detects userns-fakeroot via `/proc/self/uid_map` and accepts uid 0 only when remapped. TODO: wire into `ApptainerSpec`. |
| `nested_build` | bool (default `false`)       | Enable **NESTED** apptainer build/pull from INSIDE the agent container — a solver reproduces a capsule's pinned env itself (pull a published `docker://` image, or build a Dockerfile-derived def whose `%post` runs as root), then `apptainer exec`s it. Binds `/dev/fuse`, masks `/etc/subuid`+`/etc/subgid` (→ root-mapped + `fakeroot`-command build path; the SIF's `newuidmap` is `agent`-owned so plain `--fakeroot` FATALs), and points `APPTAINER_TMPDIR`/`CACHEDIR` at the real-disk `/tmp` (size via `tmpfs_size` — the 2G default is too small for a multi-GB image). Composes with `access: capsule` (adds **no** host-FS bind). Fail-loud if the host lacks `/dev/fuse`. Build-from-Dockerfile needs the base image to contain `/etc/subuid` (every real distro base does; busybox doesn't). Verified 2026-06-20 inside `sac-scitex.sif`. See [`runtimes/_apptainer_nested.py`](../src/scitex_agent_container/runtimes/_apptainer_nested.py). |

### `spec.engines` — several backends, one picked at start

One spec, several named backends; `--engine <key>` picks one for THAT start.
Full rationale and the four operator answers behind it: **ADR-0024**.

```yaml
spec:
  engines:
    claude:
      harness: anthropic
      model: fable[1m]
      provider: anthropic
      default: true
    qwen38-27b:
      harness: anthropic
      model: qwen38-27b
      provider: { base_url: http://127.0.0.1:18772, auth_token_env: QWEN_GATEWAY_API_KEY }
      reasoning_effort: low
      max_context_tokens: 393216
```

| Entry field          | Type                                | Description |
|----------------------|-------------------------------------|-------------|
| `harness`            | same values as `spec.harness`       | Resolves through the SAME harness registry — an engine cannot invent a harness the fleet cannot run. |
| `model`              | same as `spec.claude.model`         | The model id passed to this engine's endpoint. |
| `provider`           | same as `spec.claude.provider`      | Registered NAME or inline `{base_url, auth_token_env}`; validated by the same validator. |
| `default`            | bool                                | Exactly ONE entry may set it. With a single entry it is the default implicitly; two defaults, or two entries with none, are hard load errors naming the offenders. |
| `reasoning_effort`   | `none`\|`low`\|`medium`\|`high`     | Delivered as `SAC_ENGINE_REASONING_EFFORT`. |
| `max_context_tokens` | positive int                        | Delivered as `SAC_ENGINE_MAX_CONTEXT_TOKENS`. |
| `env`                | mapping                             | Merged OVER `spec.apptainer.env` for this engine only — the escape hatch for a gateway knob sac does not model. |

**Selecting.** `sac agents start|restart <name> --engine <key>`. START TIME
ONLY: nothing rebinds mid-session. An unknown key fails loud listing the
declared keys — it never falls back to the default.

**Refusing.** An engine that cannot be honoured (unregistered provider name,
incomplete inline provider, unset `auth_token_env` on this host, unknown
harness) REFUSES the start, naming the engine, how it was selected, what was
unhonourable, and the fix. sac never falls back to another engine.

**Reachability.** STATIC resolution runs on every start and is the whole
refusal surface by default — no sockets, so a network blip cannot ground the
fleet. `--probe-engine` (or `SAC_ENGINE_PROBE=1`) adds ONE bounded TCP connect
to the engine's `base_url`; only an ACTIVE connection refusal refuses the
start, while a timeout or DNS failure is reported as "could not tell" with a
LOUD warning and the start proceeds — never silently treated as honourable.

**Not covered yet** (each fails loud rather than dropping the engine):
`--engine` from inside a container (the host-listen broker body carries no
engine field), for an agent that lives on a peer, or with directory /
multi-agent targets.

**Migration.** A spec with only the legacy single-backend block works unchanged
and silently. Both blocks that AGREE are accepted; both that DISAGREE are a
hard load error naming both values. The migration ends when every deployed spec
declares `engines:`, at which point the legacy reading is deleted.

### `spec.claude` — SDK knobs

| Field                       | Type                                  | Description                                                       |
|-----------------------------|---------------------------------------|-------------------------------------------------------------------|
| `model`                     | alias or full ID (default `sonnet`)   | The model this agent's turns run on. Without a `provider` override it is a Claude model — see **Available models** below. WITH one it is the override endpoint's own id (e.g. `deepseek-chat`, `gpt-5.6-sol`) and the `claude-*` alias check relaxes. **The `sonnet` default is applied to every agent regardless of harness** — an Anthropic-shaped default the harness/runtime/inference layering work has not yet unwound. |
| `account`                   | string                                | Pin this agent to a stored OAuth account (`sac accounts` store-name). Mutually exclusive with `provider`. |
| `provider`                  | `{ base_url, auth_token_env }`        | Point the SDK session at any Anthropic-compatible endpoint (e.g. DeepSeek). `base_url` is the endpoint; `auth_token_env` is the NAME of the host env var holding the key (never the key). Mutually exclusive with `account`; relaxes the `claude-*` model-alias check. See ADR-0011. |
| `session`                   | `continue` \| `new-session` \| `resume`| Session strategy (default `continue` — safe fallback). Legacy aliases `continue-or-new`, `new` accepted |
| `resume_id`                 | string                                | Explicit session UUID for `session: resume`                       |
| `continue_max_age_minutes`  | int                                   | Only resume if session.jsonl is newer than N minutes              |
| `flags[]`                   | list of strings                       | Extra flags appended to `claude` invocation                       |
| `channels[]`                | `server:<name>` / `plugin:<id>@<v>`   | MCP push channels (passed as `claude --channels`)                 |
| `auto_accept`               | bool (default `True`)                 | Auto-confirm permission prompts in the TUI                        |
| `raw_options`               | dict                                  | **Escape hatch** — splatted into `ClaudeAgentOptions(**raw_options)` |

#### Available models (`spec.claude.model`)

sac validates `model` at YAML-load time, then hands it to the Claude Code
SDK (`claude --model`), which resolves the value to a concrete model. Two
shapes are accepted:

**1. Bare alias** — recommended; auto-tracks the latest version of that family:

| Alias               | Resolves to (current, 2026-05)        | When to use                         |
|---------------------|---------------------------------------|-------------------------------------|
| `opus`              | Claude Opus 4.7                        | Most capable; heaviest / slowest    |
| `sonnet`            | Claude Sonnet 4.6 — **default**        | Balanced capability and speed       |
| `haiku`             | Claude Haiku 4.5                       | Fastest, cheapest; light tasks      |
| `inherit` / `default` | SDK / host default                  | Don't pin a family explicitly       |

Append `[1m]` for the 1M-token context window where the model offers it —
e.g. `opus[1m]`, `sonnet[1m]`.

**2. Full versioned ID** — pins one exact model, no auto-tracking:
`claude-<family>-N-M[-<date>][[ctx]]`, e.g. `claude-opus-4-7`,
`claude-opus-4-7[1m]`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`.

Abbreviated forms missing the version digits (e.g. `claude-opus[1m]`) are
**rejected at validate-time** — they pass the YAML loader but the SDK
silently returns zero tokens (no API call), so the agent looks hung. The
pinned regex catches this early.

> Under a `provider` override (e.g. DeepSeek) the `claude-*` alias check
> is relaxed — use the provider's own model names instead.

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
| `autonomous.drive_until`    | string token the agent prints when done (default `DONE`)                                 |
| `autonomous.max_turns`      | int                                                                                      |
| `autonomous.kick_text`      | nudge sent when the agent pauses                                                         |

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
endpoint** instead of running an agent session in-process.
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
