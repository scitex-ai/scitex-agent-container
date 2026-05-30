# Changelog

All notable changes to `scitex-agent-container` (sac) are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **feat(jobs): federate `sac.accounts-refresh` into `scitex_dev.jobs`.**
  New provider `scitex_agent_container._jobs_plugin:provide_jobs`
  registered under the `scitex_dev.jobs` entry-point group surfaces a
  single systemd `JobSpec` (`sac.accounts-refresh`, every 2h,
  `OnBootSec=15min`, `TimeoutStartSec=120s`) running `sac accounts
  refresh --all --skip-active`. New `sac dev {cron,daemon,systemd}`
  subcommands (`list`/`install`/`uninstall`) surface sac's own `sac.*`
  jobs by delegating to scitex-dev's ecosystem aggregator; they degrade
  gracefully (upgrade hint, exit 3) when the installed scitex-dev
  predates `scitex_dev.jobs` (requires `scitex-dev>=0.16.0` in
  production). The provider import is lazy so entry-point metadata is
  install-time only.
- **feat(accounts): `sac accounts refresh --skip-active`.** With
  `--all`, excludes the stored account whose `email_address` matches the
  currently-active `~/.claude` login (`~/.claude.json`
  `oauthAccount.emailAddress`, case-insensitive) so the in-use
  refresh_token is never rotated out from under the live session. No
  active account resolvable → skips nothing and logs it. Behaviour is
  unchanged without the flag.

### Changed
- **refactor(account): extract `sac accounts refresh` into
  `cli_pkg/_account_refresh.py`** (registered onto the `account` group at
  import time, mirroring `_account_sync_live`) to keep `account_group.py`
  under the per-file line cap.
- **chore(systemd): retire the static `sac-accounts-refresh.{service,timer}`
  templates.** The unit files are now generated from the federated
  `JobSpec` via `sac dev systemd install` / `scitex-dev ecosystem systemd
  install`; `scripts/systemd/README.md` documents the new policy (the old
  templates were pinned to the superseded `--all`, every-4h cadence).

## [0.21.3] — 2026-05-28

### Added
- **feat(comms): stage 1 — symmetric federated `comms_nodes`** (ADR-0014,
  #234). New `comms_nodes` table (globally-unique `name` → `(host, a2a_port)`
  routing tuple) added to `KNOWN_TABLES` so `sac db export/import` carries
  it. `resolve_node_host` falls through to `comms_nodes` after the
  `instances` lookup, so a node living on another host's listen (e.g.
  `lead` on `ywata-note-win`) resolves correctly from a peer host.
  Symmetric ssh-pull anti-entropy via new `sac registry sync
  [--from PEER | --to PEER | --all] [--dry-run]` reusing the existing
  `sac db export/import` primitive over the operator's `peers:` ssh
  trust. Listen-startup hook registers the host's operator identity
  (from `LeadConfig`) into local `comms_nodes`; agent-lifecycle hook
  writes/tombstones the agent's row alongside the `instances` row.
  Conflict policy: name globally unique, fail-loud (`CommsNodeConflictError`)
  on `(host, a2a_port)` mismatch between sources. Adds `sac db export
  --tables TABLE[,TABLE...]` filter.
- **feat(comms): stage 2 — cross-host push via ssh-transport selector +
  ACL e2e** (ADR-0015, #236). `_node_channel._forward_to_remote` now
  selects per-host: ssh-curl when `target_host` is a member of
  `host_config.peers` (including `spartan-*` glob), HTTP otherwise.
  Generalized `_network._ssh_curl._post_via_ssh_curl(host, port, path,
  body, bearer, timeout_s)` helper shared by `/v1/turn` and
  `/agents/<name>/message:send` — same argv shape, ControlMaster reuse,
  body via ssh-stdin into `curl -d @-`. Receiver-side ACL already
  correct (admin-bearer path → `metadata.from_agent` honoured →
  `has_grant` checked against the **receiver's** local `comms_grants`).
  Closes the structural WAN gap that blocked Spartan-agent → lead push.
  No-mocks ssh-shim fixture (`tests/.../_helpers/ssh_http_shim.py`)
  installs a real `ssh` binary on `$PATH` that performs an in-process
  `httpx.post` against the destination loopback listen — substitutes
  the ssh tunnel without mocking subprocess. Stage 2 added 17 NM+TQ-
  compliant tests (5 happy-path e2e + 12 error-path); codecov on the
  diff landed at 87.50%.

### Fixed
- **docs(changelog): drop orphan `>>>>>>> origin/develop` merge marker**
  that survived a prior develop→main resolution and rendered as raw
  conflict text inside the v0.21.1 entry.

## [0.21.2] — 2026-05-28

### Added
- **feat(apptainer): `spec.apptainer.tmpfs_size`** (default `2G`) — relocate
  the container `/tmp` + `/var/tmp` onto the host filesystem via `--workdir`,
  so a `--containall` agent isn't capped at the 64 MB session tmpfs mid-run.
  Fails loud (`TmpfsSpaceError`) below the requested size; no-op when `""`
  or when the operator already passes `--workdir`. (#187)
- **feat(fleet): `sac fleet sync`** — cross-host `spec.yaml` + `to_home/**`
  audit across every peer in `config.yaml`, fail-loud on any divergence
  (no auto-merge). Worker `--collect`, `--peer`, `--only`,
  `--allow-unresolvable`, JSON output. (#207)
- **feat(acl): Phase-3 server-managed ACL enforcement** (ADR-0010) —
  per-spec `spec.comms` (outbound/inbound siblings/parent allow|deny,
  `a2a.listen`) and `spec.lineage` (`group`, `may_spawn`), persisted to
  `state.db` and enforced at the listen send path and the spawn gate. (#206)
- **docs(readme): Models section** + spec-reference "Available models"
  (opus/sonnet/haiku aliases → current 4.x families, `[1m]` context, full
  versioned IDs, provider override); skills `spec.claude` field table
  (account + provider); ADR-0012; refreshed CLI surface (fleet/host/accounts).
- **feat(base-image): apt-based fd/rg/bat/eza** with Debian symlinks. (#205)

### Fixed
- **fix(mcp): account tools call renamed `accounts` subcommands** — the MCP
  `account_show` / `quota_watch` tools invoked the removed `account` /
  `quota watch` CLI verbs; repointed at `accounts status` /
  `accounts watch-quota`. (#231)
- **docs(skills): purge v2-era field references from `_skills/scitex-agent-container/`** —
  brought seven skills in lockstep with the v3 validator (`config/_validation.py`),
  which strictly rejects `spec.remote`, `metadata.name`, top-level
  `spec.model`, `spec.skills`, and `dot_claude`. Highlights: full rewrite
  of `11_remote-deploy.md` to `spec.host` / `spec.hosts` + `sac --on <peer>`
  dispatch; YAML example in `01_config-v3.md` no longer ships
  `spec.skills` / top-level `spec.model` / `multiplexer-alive`; A2A
  AgentCard mapping in `07_a2a-protocol-extension-fields.md` points at
  the dir-as-SSoT name source and the file-based skill layout;
  `19_full-agent-troubleshooting.md` replaces the `spec.skills.required` /
  `spec.skills.available` table with the `to_home/.claude/skills/<id>/`
  delivery mechanism. Added validator tests covering the unknown-spec-field
  catch-all and the `metadata.name` / `dot_claude` / `spec.skills`
  rejection messages; relocation-vs-unknown messages stay distinct.

## [0.21.1] — 2026-05-26

### Added
- **feat(ssh): connection multiplexing across all sac→peer ssh calls**
  — every sac call that shells out to ssh now prepends
  `-o ControlMaster=auto -o ControlPersist=60s -o ControlPath=<dir>/%C`
  so concurrent calls against the same peer share one TCP+SSH master.
  Fixes (a) "control socket dir is read-only" inside apptainer SIFs
  where the default `~/.ssh/sockets` ControlPath lands on the overlay,
  and (b) silent drops when fanning out across hosts that cap
  per-user concurrent sessions (Spartan `MaxSessions`,
  sshd `MaxStartups`). The ControlPath dir defaults to
  `${TMPDIR:-/tmp}/.sac-ssh-cm` (writable inside apptainer); override
  with `$SAC_SSH_CONTROL_DIR`; opt out entirely with
  `SAC_SSH_CONTROL_MASTER=0`. New helpers
  `scitex_agent_container._state.host_config.ssh_control_options()`
  and `ssh_control_options_str()`, applied centrally in `build_ssh_argv`
  and at three direct-ssh call sites (`_network.peer._post_turn_via_ssh`,
  `cli_pkg.priority_cmds._ssh_start_agent`,
  `cli_pkg._send_preflight.default_ssh_runner`). New CLI
  `sac host ssh-opts` prints the flags shell-quoted for use in agent
  prompts as `ssh $(sac host ssh-opts) host cmd`. See
  `_skills/scitex-agent-container/11_remote-deploy.md` §
  "SSH connection multiplexing". Failure mode is fall-through: if the
  control dir can't be created (read-only mount, ENOSPC) the helper
  returns `[]` and ssh argv stays byte-identical to pre-patch.

## [0.21.0] — 2026-05-25

### Added
- **feat(provider): vendor-agnostic Anthropic-SDK backend override (PR #208)** —
  `spec.claude.provider` with `base_url` + `auth_token_env` points a sac
  agent's Claude-SDK session at any Anthropic-SDK-compatible endpoint
  (DeepSeek first). Host-side `runtimes/_apptainer_provider.py` emits
  `ANTHROPIC_BASE_URL` + `SAC_ANTHROPIC_API_KEY` + `CLAUDE_CONFIG_DIR`
  at start; auth resolution via `scitex_config` cascade
  (shell-export > `$HOME/.env` > default → fail-loud
  `ProviderEnvError`). `claude-*` model alias check is relaxed under a
  provider override; `provider` + `spec.claude.account` are mutually
  exclusive. New: `config/_provider_types.ProviderSpec`,
  `runtimes/_apptainer_provider.py`, `runtimes/_apptainer_auth.py`,
  ADR-0011, `examples/agents/deepseek-agent/spec.yaml`. 73 targeted
  tests; full pytest matrix green.
- **chore(audit): exempt sac from §6 MCP-Python parity (PR #209)** —
  adds `[tool.scitex_dev] mcp_parity_exempt = true` to pyproject for
  the audit-cli §6 check. sac's MCP tools mirror CLI subcommands, not
  top-level Python APIs (4 orphan tools: `agent_spawn`,
  `list_python_apis`, `quota_watch`, `subagent_get_state`). Closes the
  only develop-shared audit-conformance violation;
  `tests/develop/test_audit.py::test_audit_all_clean` now green on
  develop.

### Added
- **feat(account): credential auto-sync substrate (`sac accounts
  sync-live` / `watch-live`)** — keep the per-account store fresh the
  moment the operator runs `claude /login`, with zero manual `sac
  accounts save`. `sync-live` reads the live `~/.claude/.credentials.json`
  + the active email from `~/.claude.json`, derives the store-name
  (email slugified, e.g. `ywatanabe@scitex.ai` → `ywatanabe-scitex-ai`),
  and atomically snapshots the live cred in when the matching store is
  absent / older / expired (idempotent no-op otherwise). `watch-live`
  is the always-on daemon: watches the live credential (inotify via
  `inotifywait` when available, else a poll loop) and runs the engine on
  every change, logging each sync to stderr or
  `~/.scitex/agent-container/runtime/logs/creds-watch.log`. New
  `_account/creds_sync.py` (engine) + `_account/creds_watch.py`
  (watcher).
- **feat(account): `sac accounts list` credential-freshness column** —
  every stored account now shows `VALID (+Xh)` / `EXPIRED (-Xh)` /
  `ABSENT` read OFFLINE from the snapshot's `expiresAt`, so rotted
  stores are visible at a glance. The `--json` output gains `freshness`
  and `freshness_hours` fields. New `_account/creds_sync.account_freshness`.

### Fixed
- **fix(account): pinned-account credential resolution now fails loud**
  — when `spec.claude.account` names a store that is ABSENT or its
  credential is EXPIRED, `sac agents start` now aborts with
  `PinnedAccountError` carrying the exact remedy (`claude /login` to
  that account + `sac accounts sync-live`). Previously it silently fell
  back to the host live file (a *different* account) or launched with a
  stale token — handing the agent the wrong identity. A pinned agent
  must never silently fall back. (`runtimes/_apptainer_creds.py`)

## [0.20.0] — 2026-05-24

### Added
- **feat(account): per-agent OAuth account assignment
  (`spec.claude.account`)** — pin an agent to a specific saved
  Anthropic account (from `sac account list`). Multi-account
  load-balance enabler: agents on distinct accounts no longer collide
  on one server-side rate limit. Frozen boot-copy mechanism — the
  apptainer runtime copies the named account's `.credentials.json`
  snapshot into the agent's own state dir at start and binds the copy
  RW, so two agents on two accounts never fight one mount and a host
  `/login` never moves a pinned agent. Takes effect on next
  start/restart; `account=""` (default) keeps the host live-file
  behaviour. New `runtimes/_apptainer_creds.resolve_cred_file`;
  load-time soft-WARN when the named snapshot is absent (never fails).
- **feat(account): `sac account list` plan/tier + usage columns** —
  OFFLINE plan label (Pro / Max 5x / Max 20x) and rate-limit tier for
  every stored account, read from the snapshot's whitelisted fields;
  CACHE-ONLY usage (`—` until a per-account usage cache exists). New
  `_state/account_store.read_account_plan` /
  `read_account_usage_cache`.
- **feat(status): pinned-account display** —
  `resolve_agent_account_label(assigned_account=)` so a pinned agent
  shows `<name> (<email>)` in `agent list` / `agent status` regardless
  of the host's current `/login`.

### Fixed
- **fix(spec): migrate bundled `sdk-test` spec off removed v3
  top-level fields** (`runtime: docker→apptainer`; `image`/`model`
  under their engine blocks; dropped `dockerfile`).

### Changed (BREAKING)
- **refactor(api): drop ``reply`` field; ``text`` is the single canonical
  field** — the A2A sidecar's ``POST /v1/turn`` response now returns
  ``{"text", "session_id", "exit_after", "metadata"}`` only; the
  back-compat ``reply`` alias (introduced alongside the bounded-timeout
  fix in 63deaee) is removed. Affects all consumers:
  - ``sac peer post-turn --json`` now emits ``{"text", "exit_after"}``
    (was ``{"reply", "exit_after"}``). Pipe consumers using
    ``jq -r .reply`` must update to ``jq -r .text``.
  - ``sac listen``'s live-runner forward response carries ``text``
    instead of ``reply``.
  - ``a2a_proxy`` runner's non-JSON upstream fallback wraps the
    upstream body under ``text`` instead of ``reply``.
  - The Python ``_network.peer.post_turn_to_url`` parses ``payload["text"]``
    explicitly; a missing key now raises ``PeerError`` (no silent
    ``.get`` fallback). Callers must adapt.

### Added
- **feat(mcp): expose `agent_send` tool** — the MCP server now ships
  37 tools (up from 36); the new `agent_send` lets a lead drive
  another agent via a prompt instead of shelling out to `sac agents
  send`. Returns a structured `{status, response_text,
  response_metadata}` dict (status one of `"ok"`, `"error"`,
  `"timeout"`); cross-host agents route through the same ssh+curl
  control plane the CLI uses. Library-facing helper lives at
  `cli_pkg/_send.py::send_to_agent` so the HTTP / ssh logic isn't
  duplicated.
- **feat(apptainer): declarative overlay auto-create via
  `spec.apptainer.overlay_size`** — operators no longer have to run
  `apptainer overlay create --size N proj-<peer>.overlay.img` by hand
  for every new peer. Set `spec.apptainer.overlay_size: "5G"` (units
  M/MB/G/GB) alongside `spec.apptainer.overlay: <path>` and sac
  creates the overlay image on first launch if it's missing. Gated by
  the new `overlay_create_if_missing` flag (default `true`); set to
  `false` to keep "operator must pre-create" semantics. When
  `overlay_size` is empty AND the overlay is missing, sac now fails
  earlier with a clear `FileNotFoundError` ("set
  `spec.apptainer.overlay_size` for auto-create, or pre-create with
  `apptainer overlay create`") instead of letting apptainer error out
  cryptically at exec time. Behaviour for specs with existing overlay
  files is unchanged. See `docs/isolation.md` §7.

### Changed
- **docs/spec-reference.md** — verified field-by-field against
  `config/_parsers/` + `config/_validation.py`; corrected
  `spec.runtime` (REQUIRED → optional with default), `spec.dot_claude`
  (no auto-discover), `spec.startup_commands[]` (list of dicts not
  strings), `spec.listen` (LIST of port-declarations, not host-listen
  override), `spec.telegram.*` (field names: `bot_token_env` /
  `allowed_users` / `auto_connect` / `greeting`, not `enabled` /
  `chat_id`), and apptainer `image` (REQUIRED → optional). Flagged
  `spec.apptainer.relaxed` / `fakeroot` as `(DESIGN — not yet
  implemented in parser)` and noted that `spec.multiplexer` /
  `spec.env-file` are parsed but currently missing from
  `_KNOWN_SPEC_KEYS` (parser/validator drift to fix in a follow-up).
  Companion audit at `docs/spec-reference.audit.md`.

### Removed
- **docs/agent-spec-schema.yaml** moved to
  `docs/legacy/agent-spec-schema-cld-v1.yaml`. It documented the
  pre-v3 `cld-agent/v1` schema (`metadata.name`, `runtime: claude-code
  | slurm | slurm-tenant`, `container:` block, `orochi:` block) which
  is now rejected at load time. Kept under `docs/legacy/` for
  historical reference only.

### Added
- **telegram fold (Phase 2 + Phase 3)** — the Telegram fold is now a
  first-class sac feature. `TelegramBridge` is fully ported from orochi
  (`_telegram/_bridge.py`): aiohttp long-poll against the Telegram Bot
  API, `allowed_users` filter enforced on inbound updates (empty list
  fails closed), and graceful shutdown that closes the API session and
  releases the singleton lock. The per-bot-token `flock`
  (`_telegram/_lock.py`) lives at
  `~/.scitex/agent-container/runtime/telegram/<token-hash>.lock` and
  reclaims the lock when the recorded PID is dead — the failure mode
  that the standalone telegrammer hits and that previously required a
  manual `rm`. The bridge boots from `_mcp/server.py` via
  `_maybe_boot_telegram_bridge`, which only fires when
  `LEAD_TELEGRAM_AUTH_TOKEN` is set in the env (i.e. only on the lead
  session — subagents inherit a sanitised env and get a structured
  `{"error": ...}` response from the tools). The six transport tools
  (`telegram_send`, `telegram_reply`, `telegram_react`,
  `telegram_edit_message`, `telegram_download_attachment`,
  `telegram_send_document`) are wired to the in-process bridge and
  registered by default — opt out via
  `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=0` (previously opt-in via
  `=1`). New `sac.telegram` Python API submodule re-exports the verbs
  for §6 parity. Inbound messages emit `notifications/claude/channel`
  payloads shaped `{"content": ..., "meta": {"source": "telegram",
  "chat_id": ..., "message_id": ..., "user_id": ..., "username": ...,
  ...}}`; these only render when the Claude Code launcher is invoked
  with `--dangerously-load-development-channels
  server:scitex-agent-container` — see `docs/design/telegram-fold.md`
  for the launcher dependency note + WARN-log signal.
- **telegram fold (Phase 1)** — design + scaffolding for folding
  claude-code-telegrammer's transport tools into sac MCP (Option A from
  `GITIGNORED/dev/05_sac-mcp-telegram.md`). New `_telegram/` package with a
  `TelegramBridge` skeleton (Phase 2 port target documented in the module
  docstring) and `_mcp/_tools/_telegram.py` with 6 transport tool stubs
  (`telegram_send`, `telegram_reply`, `telegram_react`,
  `telegram_edit_message`, `telegram_download_attachment`,
  `telegram_send_document`). Registration is feature-flagged off behind
  `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=1` — no user-visible behaviour
  change yet. Design doc at `docs/design/telegram-fold.md`.
- **F-CS15** — `sac mcp` server. New `_mcp/` package + `cli_pkg/mcp_cmds.py`
  Click group exposing `start`, `doctor`, `list-tools`, `install` against a
  FastMCP server with 36 `sac_*` tools mirroring the CLI surface (agent /
  db / host / image / template / account / skills / introspection). Install
  with `pip install scitex-agent-container[mcp]`.
- **F-CS3** phase 2 — runner-side autonomous loop: SDK runner consumes
  `spec.autonomous` and drives turns until `drive_until` matches or
  `max_turns` is reached. New `_runners/claude_session._autonomous_loop`
  coroutine + `--autonomous-*` CLI args; container.py forwards the spec.
- **OAuth credentials synthesis** — `provision_anthropic_auth` writes a
  minimal `~/.claude/.credentials.json` when given a `sk-ant-oat-*`
  access token via `SAC_ANTHROPIC_API_KEY`, so Pro/Max plans work in
  containers without the host file.

### Changed
- **F-CS17** stage 3a–3d — CLI/TUI runtime, SLURM runtime, action/context
  pane abstractions, and the legacy Dockerfile all removed. `sac` is now
  container-only: docker / podman / apptainer engines, the SDK-persistent
  base image, and the standalone Python runner under `_runners/`.
- **Auth handoff renamed** to `SAC_ANTHROPIC_API_KEY`. The previous
  `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY[_OAUTH]` envs are gone.
  `provision_anthropic_auth` only honours `ANTHROPIC_API_KEY` when set
  explicitly by the operator; otherwise it bridges (api-key form) or
  synthesises a credentials file (oat- form) from `SAC_ANTHROPIC_API_KEY`.
- **Container daemon argv** — drops `--rm`; lifecycle.stop now does an
  explicit `docker rm -f` after stop. Adds `--env HOME=/tmp` so the
  bundled `claude` binary has a writable home even when the host UID
  has no `/etc/passwd` entry inside the image. Forwards
  `SAC_ANTHROPIC_API_KEY` (and `ANTHROPIC_API_KEY` when explicit).
- **F-CS16** — schema flatten (`spec.image`, `spec.dockerfile` top-level),
  auto-build at start, validator hard-errors on legacy runtimes.

### Removed
- F-CS17: `runtimes/slurm{,_tenant,sbatch_spartan}.py`, the entire CLI/TUI
  stack (`context_manager`, `actions/{compact,nonce_probe}`, `auto/response`,
  `cli_pkg/action_cmds`), the legacy `containers/Dockerfile`, and all
  associated tests (~7,500 LOC).

## [0.13.0] — pre-rewrite snapshot

Reference tag for the state immediately before F-CS17. See `git log v0.13.0`
for the prior history.
