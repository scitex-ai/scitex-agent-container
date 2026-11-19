# Explicit grants (`spec.grants`)

Every SAC agent used to receive a large set of host authorities implicitly:
fleet binds, the sac control plane, host secrets, the host network, the
operator's `~/.claude`, and, through its groups, host exec. `spec.grants`
turns each of them into an explicit, named grant.

```yaml
spec:
  grants:
    host_exec: false          # POST /v1/host_exec (arbitrary host argv)
    sac_control: true         # sac-builtin MCP + server:sac + listen URL/bearer/token
    host_network: true        # share the host network namespace
    host_binds: [cards, cct_store]
    fleet_env: true           # fleet default env, PG/board identity, …
    host_claude: [commands, skills, baseline]
    secrets: [envrc, github_token]
    host_model_auth: true     # host ~/.claude OAuth dir, host-env model keys
```

## Semantics

| `spec.grants` | Meaning |
|---|---|
| **absent** | **Legacy.** Every implicit authority applies exactly as before: the rendered argv, env and home are byte-identical to the release before this key existed. `sac agents explain` labels each grant *implicit (legacy default)* and `sac agents check` warns that the spec relies on a default that a later release flips to deny. |
| **present** | **Deny by default.** The agent receives only what the block grants. An omitted key is withheld; `grants: {}` withholds everything. |

## The grant keys

| Key | Type | Grants |
|---|---|---|
| `host_exec` | bool | `POST /v1/host_exec`: run arbitrary argv on the host as the operator. Needs `sac_control`. The listen server checks it per request (in addition to the eligible-group gate), reading the caller's **fleet-scope** spec, never a project-scope copy. An unreadable spec is refused. |
| `sac_control` | bool | The injected `scitex-agent-container` MCP server, the `server:sac` channel, and the `SAC_LISTEN_BASE_URL` / `SAC_LISTEN_BEARER` / `SAC_AGENT_TOKEN` env. The `sac-builtin: off` label still opts out of the builtin injection when granted. Declaring `server:sac` or the `scitex-agent-container` MCP by hand without the grant is a validation error. |
| `host_network` | bool | Share the host network namespace (sac listen, the genai tunnel, Postgres, peer a2a sidecars, the Hermes API port). See [Host network](#host-network). Contradicts `spec.sandbox` (validation error). |
| `host_binds` | list | Named fleet binds: `cards` (`~/.scitex/cards`), `cct_store` (`~/.scitex/claude-code-telegrammer`), `emacs_handoff` (`/tmp/emacs-claude-code:ro`), `host_tmp` (`/tmp:/tmp/host:ro`), `quota_cache` (the quota cache file + `CCT_QUOTA_CACHE_PATH`). |
| `fleet_env` | bool | `fleet_default_env` (e.g. `SCITEX_STORE_DSN`), the name-derived `SCITEX_CARDS_AGENT_ID` and `PGUSER`, `SCITEX_AGENT_CONTAINER_RUNTIME_DIR`, `SCITEX_AGENT_CONTAINER_YAML_DIRS`, `SCITEX_GENAI_BASE_URL`, `DIRENV_CONFIG`; through `effective_env` also the fleet part of Hermes MCP runtime env and the pgpass it implies. What `spec.env` itself declares is unaffected. |
| `host_claude` | list | `commands` (copy host `~/.claude/commands`), `skills` (link host `~/.claude/skills/{ywatanabe,scitex}`, and Hermes installed-package skills), `merge` (developer deep-merge of host `~/.claude/{commands,skills,hooks}`; still needs a full-developer agent), `baseline` (the `user-shared` / `project-shared` to_home layers and their `.envrc`). Without `baseline`, naming a shared layer in `to_home_layers` is a validation error. |
| `secrets` | list | `envrc` (host-side fold of the `.envrc` cascade into `$HOME/.env`), `github_token` (`GITHUB_TOKEN`/`GH_TOKEN` from the pool), `cct_token` (`CCT_BOT_TOKEN` from the pool for a telegrammer agent), `operator_pool` (the Hermes profile operator secrets; `spec.secret_scope` still picks full vs capsule). |
| `host_model_auth` | bool | Host-held model credentials: the unpinned host `~/.claude` OAuth dir bind, host-env `ANTHROPIC_API_KEY`/`SAC_ANTHROPIC_API_KEY` forwarding, the openai harness's host key (without it an openai-harness launch is refused), codex's host `CODEX_HOME` override, key and routing env. Credentials the spec **declares** stay: `claude.account` (pinned snapshot), `credentials_file`, a `provider`, a codex subscription account. So the three Claude states are: `true` → host OAuth dir; `false` + `claude.account` → pinned account only; `false` alone → none. |

### Host network

A non-sealed apptainer agent always shares the host network namespace; SAC
has no curated network short of the sealed run's allowlist gateway.
Isolating such an agent with `--network none` would leave it without its
model endpoint — a silently broken agent rather than a refused one. So an
explicit `grants:` block without `host_network: true` **refuses** a
non-sealed launch (`sac agents explain` shows `START WILL REFUSE`), and the
two honest choices are "grant `host_network`" or "declare `spec.sandbox`".
A sealed agent keeps its egress allowlist and must not grant `host_network`.

## Inventory → grant mapping

IDs refer to the implicit-authority inventory (groups A–H). Every item maps
to exactly one grant or is listed as not an authority, with the reason.

| ID | Authority | Grant / status |
|---|---|---|
| A1 | `<state>/home` → `/home/agent` | always on — the agent's own home |
| A2 | `$HOME/.env` via `--env-file` | always on — the delivery channel; what lands in it is `secrets` |
| A3 | `<state_dir>` → `/state/<name>` | always on — the agent's own state |
| A4 | quota-cache bind + `CCT_QUOTA_CACHE_PATH` | `host_binds: [quota_cache]` |
| A5 | `~/.scitex/cards` | `host_binds: [cards]` |
| A6 | `~/.scitex/claude-code-telegrammer` | `host_binds: [cct_store]` |
| A7 | `/tmp/emacs-claude-code:ro` | `host_binds: [emacs_handoff]` |
| A8 | host `/tmp` → `/tmp/host:ro` | `host_binds: [host_tmp]` |
| A9 | per-agent `/uvwork` | always on — the agent's own scratch |
| A10 | per-agent scratch `--workdir` | always on — the agent's own scratch |
| A11 | unpinned host `~/.claude` → `/tmp/sac-claude` (+ `CLAUDE_CONFIG_DIR`) | `host_model_auth` (a pinned `claude.account` is declared, stays) |
| A12 | `.credentials.json` file bind | explicit — `claude.account` / `credentials_file` in the spec |
| A13 | provider config dir | explicit — `spec.claude.provider` |
| A14 | codex home (`<state>/codex-home`) | always on (own state); the host `CODEX_HOME` override is `host_model_auth`; subscription auth is the declared account |
| A15 | `nested_build` / `volumes` | explicit — opt-in spec keys |
| B1 | host network namespace | `host_network` |
| B2 | `apptainer.relaxed` | explicit spec key (escape hatch) |
| B3 | `raw_args` passthrough | explicit spec key (escape hatch) |
| C1 | `SCITEX_AGENT_CONTAINER_RUNTIME_DIR` | `fleet_env` |
| C2 | `SCITEX_STORE_DSN` / `fleet_default_env` | `fleet_env` |
| C3 | name-derived `PGUSER` | `fleet_env` |
| C4 | name-derived `SCITEX_CARDS_AGENT_ID` | `fleet_env` |
| C5 | `SAC_LISTEN_BASE_URL` | `sac_control` |
| C6 | `SAC_LISTEN_BEARER` (+ `SAC_AGENT_TOKEN`) | `sac_control` |
| C7 | `SAC_NAME` | always on — identity, not authority |
| C8 | `DIRENV_CONFIG` | `fleet_env` |
| C9 | `UV_PROJECT_ENVIRONMENT` | always on — tool config |
| C10 | `SCITEX_GENAI_BASE_URL` | `fleet_env` |
| C11 | `MCP_TIMEOUT` | always on — tool config |
| C12 | `SAC_SPEC_ENV_KEYS` | always on — names only; follows whatever env the grants leave |
| C13 | `SCITEX_AGENT_CONTAINER_YAML_DIRS` | `fleet_env` |
| C14 | telegrammer wake env | explicit — wiring of a declared `server:claude-code-telegrammer` channel |
| C15 | engine parameters | explicit — the selected engine |
| C16 | Hermes wiring | always on — harness wiring |
| C17 | OpenCode profile env | always on — harness wiring |
| C18 | vendor-credential denial | always on — removes authority |
| D1 | host `ANTHROPIC_API_KEY` / `SAC_ANTHROPIC_API_KEY` forwarding | `host_model_auth` |
| D2 | provider key / base URL | explicit — `spec.claude.provider` (its `auth_token_env`) |
| D3 | openai harness host key + passthrough | `host_model_auth` (refused without it) |
| D4 | codex host key + routing env | `host_model_auth` |
| E1 | host exec | `host_exec` |
| E2 | sac-builtin MCP | `sac_control` |
| E3 | `server:sac` channel subscription | `sac_control` |
| E4 | a2a sidecar | explicit — `spec.a2a` / `comms.a2a.listen`; its reachability is `host_network` |
| E5 | Hermes API server | always on for a headless Hermes profile (own key in own state); its reachability is `host_network` |
| F1–F5 | harness permission modes | **not a grant** — see below |
| G1 | host `~/.claude/commands` copy | `host_claude: [commands]` |
| G2 | host `~/.claude/skills` links | `host_claude: [skills]` |
| G3 | developer host deep-merge | `host_claude: [merge]` |
| G4 | shared to_home baseline layers | `host_claude: [baseline]` |
| G5 | `.envrc` cascade fold | `secrets: [envrc]` |
| G6 | `CCT_BOT_TOKEN` from the pool | `secrets: [cct_token]` |
| G7 | `GITHUB_TOKEN` / `GH_TOKEN` from the pool | `secrets: [github_token]` |
| G8 | `spec.env` baked into MCP entries | explicit — `spec.env` + `mcp_servers.<n>.inherit_env` |
| H1 | Hermes operator secret pool | `secrets: [operator_pool]` (tier: `spec.secret_scope`) |
| H2 | Hermes sac profile env | `sac_control` (written only when the sac MCP is present) |
| H3 | Hermes CCT profile env | `secrets: [cct_token]` (or `envrc`, whichever put the token in `.env`) |
| H4 | Hermes MCP runtime env | `fleet_env` for the fleet part; the rest is `spec.env` |
| H5 | pgpass materialisation | follows `fleet_env` (the DSN that implies it) or `spec.env` |
| H6 | Hermes installed-package skills | `host_claude: [skills]` |

**Why harness permission modes (F) are not a grant.** F1–F5 (`bypassPermissions`,
codex `danger-full-access`, Hermes `approval_mode: off`, opencode `never`,
Claude settings) decide whether the harness asks the model's tool calls for
approval *inside* the container. They grant nothing beyond what the
container already holds; the container boundary — which the grants above
define — is the authority boundary. Where they are configurable they are
explicit harness configuration (`available_harnesses.<key>`), and the
grants block deliberately does not duplicate them.

## Migration (infer → paste → check)

```bash
sac agents grants infer scitex-dev      # prints the grants: block that
                                        # reproduces today's authority
# paste it under spec: in spec.yaml, then prove it:
sac agents grants check scitex-dev      # 0 diff = PASS
```

`infer` renders the spec without `grants:` (twice — lines that differ
between two identical renders are volatile and ignored), then once per grant
with every grant except that one. A grant whose removal changes the launch
surface is inferred. `host_exec` has no launch surface, so it is inferred
from the same eligible groups the listen server applies.

If the legacy render is itself refused (a pre-existing spec problem, e.g. a
Hermes spec without a verified `AGENTS.md` projection), the authorities past
the refusal cannot be observed: `infer` then prints the complete block — the
legacy default by construction — and `check` reports `INCONCLUSIVE` instead
of `PASS`. Fix the spec, then infer again for the minimal block.

`check` diffs the legacy surface against the spec plus the inferred block:
the redacted argv, the container env names (`--env` and every `--env-file`),
and the materialised home (file list, symlink targets, key names of
secret-shaped files, digests of the others). Both commands render into a
throwaway directory (`--render-dir`), never the agent's real state, overlay
or `~/.scitex`, and launch nothing. Secret values are never printed.

## Rollout

1. **0.43.0 (this release)** — the keys exist; a spec without `grants:`
   behaves exactly as before.
2. **Fleet specs** — every fleet agent gains its inferred block, with zero
   argv/env/home difference per agent (`sac agents grants check`).
3. **A later minor release** flips the default: a spec without `grants:`
   is treated as `grants: {}`.

## Where it shows up

- `sac agents explain NAME` — a `HOST EXEC` banner (loud when held), then
  every grant with its source: *explicit* or *implicit (legacy default; will
  be denied when the default flips)*.
- `sac agents check NAME` — warns on host exec (explicit or implicit) and on
  a spec with no `grants:` block.
