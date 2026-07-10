# Changelog

All notable changes to `scitex-agent-container` (sac) are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **refactor(accounts): `sac accounts list` — dedupe table vs usage
  bars** (operator directive 2026-07-11: "Stored accounts と Usage
  bars で duplicated info を出すな / Usage bars で書けないものだけ
  Stored Accounts に書け / ID <-> Email 対応は要らない；Plan も
  いらない"). Presentation-layer only; the `--json` schema is
  untouched (still carries `email_address`, `plan_label`, raw usage):
  - Stored-accounts table is now exactly
    `Account | Status | Last Update` — the Email column (IDs are
    email-derived slugs), the Plan column and the 5h%/7d% columns
    (which duplicated the bars and wrapped the table at normal
    terminal widths) are gone. Status keeps the live token TTL
    (`VALID +2h26m`).
  - The usage-bars block owns the percentages AND gains the compact
    per-window reset hints that used to clutter the table cells:
    `5h [██████░░░░░░░░░░░░░░]  29% (→09:19)   7d [...]  66% (→Sun 21h)`.
    Missing 5h hints are space-padded so the 7d bars stay vertically
    aligned. The `(in Xh Ym)` countdown qualifier was dropped with
    the table cells it annotated (the bars stay compact per the
    operator's verbatim example).
  - The rolling-window legend now prints below the bars (the surface
    it explains); the fleet effective-utilization footer and the
    single-account "Claude Code account" header block are unchanged.
  - `account_group.py` extraction: the `list` command body moved to
    `cli_pkg/_account_list_cmd.py` (`register_list_command`), the
    same pattern as `_account_refresh.py`, keeping the group
    orchestrator under the per-file line cap.

## [0.21.11] — 2026-06-09

PATCH release. Re-cut of v0.21.10's accounts-list redesign after the
v0.21.10 tag's release workflow failed on `test_audit_all_clean` (PR
#349 itself introduced 3 STX-TQ007 violations in the new
`test__account_list_render.py` plus a stale SK-301 SKILL.md WARN that
was still on develop). v0.21.10 exists as a "ghost" tag — no PyPI
artifact, no GitHub Release; operators install
`scitex-agent-container==0.21.11` for the accounts-list redesign
PLUS the cleanup that unblocks the release pipeline.

### Fixed
- **chore(repo): clean 17 pre-existing audit-all violators** (#348,
  unblocks #341/#344/release). Splits 7 STX-TQ007 violators in
  `tests/scitex_agent_container/config/test__loaders_model_chain.py`
  (12→29 tests, 1-assert each via shared Arrange+Act helper); 4
  STX-TQ002 (AAA-marker) violators in
  `tests/scitex_agent_container/_runners/test__session_inbox.py`;
  relocates `tests/scitex_agent_container/containers/
  test_apptainer_scitex_def_libxcb.py` to `tests/integration/` to
  satisfy PS-204 (orphan-test mirror rule) while preserving the
  libxcb / libgl1 / libglib2.0-0 regression guards; adds the
  required `## <N> Interfaces` H2 and Four Freedoms blockquote to
  `README.md` (PS-107 / PS-110); trims `SKILL.md` from 6181→6070
  bytes to clear SK-301 (74-byte margin under the 6144 budget);
  fixes the post-relocation `parents[2]` repo-root walker in the
  moved apptainer-def test; and splits 3 STX-TQ007 violators
  introduced by #349 in `test__account_list_render.py`. No
  behaviour change — only test shape and docs.

### Included from v0.21.10 (ghost tag — not on PyPI)
- **feat(accounts): `sac accounts list` window-reset display**
  (operator gripe via lead, 2026-06-09). See the v0.21.10 entry
  below for the full description. Headers, per-row reset hints,
  and rolling-window legend all ship in v0.21.11.

## [0.21.10] — 2026-06-09

### Changed
- **feat(accounts): `sac accounts list` window-reset display**
  (operator gripe via lead, 2026-06-09). Three operator-visible
  clarifications to the Stored-accounts table; no behaviour change
  in the JSON path (`sac accounts list --json` schema is stable):
  - Column header `As-of` renamed to `Last Update` — the operator
    could not parse the abbreviation.
  - 5h%/7d% cells now carry an inline reset hint computed from the
    Anthropic OAuth usage API's `resets_at` field (parsed by
    `_account.claude_usage` into `reset_at_5h` / `reset_at_7d`).
    Example shapes: `42% (→21:05)` for the 5-hour rolling window,
    `15% (→Thu 17h)` for the 7-day rolling window. Local-tz
    precedence chain unchanged
    (`SCITEX_AGENT_CONTAINER_TZ` > `TZ` > system local).
  - When the upstream API did NOT return reset timestamps (older
    caches / API outage) the CLI prints a one-line legend below
    the table — `5h = rolling 5-hour window; 7d = rolling 7-day
    window` — so the operator still knows the windows are rolling
    rather than calendar-day. Headers stay compact so the table
    fits a typical ~120-col terminal.

  Implementation lifted the pure formatting helpers out of
  `_account_list_render.py` into a sibling `_account_list_format.py`
  so neither module exceeds the 512-line per-file cap (the renderer
  re-exports the helpers so existing imports keep working).
  Two new fields on `AccountRow`: `reset_at_5h` / `reset_at_7d`
  (optional, default `None`). `build_stored_rows` propagates them
  from the per-account `usage.json` cache.

## [0.21.9] — 2026-06-01

### Fixed
- **fix(channel): suppress empty-content delivery acks at the sender**
  (#260, lead handoff). Killed the empty-ack ping-pong spam hitting
  every inbox (incl. lead's). New ``_channel_ack_filter`` +
  ``_channel_auto_ack`` modules; sender-side filter drops empty-
  content acks before the wire so the receiver never sees them.
  Rate-limit env knobs (``SAC_AUTO_ACK_RATE_MAX`` /
  ``SAC_AUTO_ACK_RATE_WINDOW_S``) gate spammy auto-ack loops.
- **fix(smoke): update deny-row assertions for task #27 two-row
  contract** (#281). v0.21.8's pypa-publish failed because the
  smoke test ``test_listen_denied_send_persists_exactly_one_
  channel_events_row`` pinned the pre-task-#27 single-row
  contract; task #27 (block/unblock approve-flow, landed in
  v0.21.8's contents) intentionally added a second row (the
  ``approval_prompt`` push). Smoke test updated to the new two-
  row contract + 5 new assertions on the prompt body (embeds
  both ``sac a2a unblock`` and ``sac a2a block``, does NOT leak
  the sender's body). No production code change; v0.21.8's
  feature set still ships here.

v0.21.9 = v0.21.8's full feature set (#278 + #279, task #27 ACL
approve-prompt flow) re-cut after #260 + #281 unblocked the
release pipeline. The v0.21.8 tag exists on GitHub but is a
"ghost" — no PyPI artifact and no GitHub Release; operators
install ``scitex-agent-container==0.21.9`` for the same feature
set + the empty-ack fix + the smoke-test contract update.

## [0.21.8] — 2026-06-01

### Added
- **feat(acl): block/unblock approve-prompt flow** (#278 + #279,
  operator-requested via lead — task #27). Cross-group denied
  sends now emit ONE receiver-facing prompt embedding BOTH
  ``sac a2a unblock <s> <t>`` and ``sac a2a block <s> <t>`` so
  the receiver picks the verb. Dedupe: repeats from the same
  (sender, target) pair while pending DO NOT re-prompt. UNBLOCK
  writes ``comms_grants`` + removes any ``comms_blocks`` + clears
  the pending row (sender's future messages pass; the original
  denied message is NOT replayed — sender resends). BLOCK writes
  ``comms_blocks`` (block precedence over grant) + clears the
  pending row (sender's future attempts silently dropped — no
  receiver push, no approve-prompt re-fire, sender still gets
  403). New CLI verbs ``sac a2a unblock`` and ``sac a2a block``;
  legacy ``sac a2a grant`` aliased to unblock. The push body
  intentionally does NOT leak the denied message content —
  receivers decide on identity, not on content. Intentionally
  drops the earlier-design TTL knob + latest-wins / replay
  machinery per the operator's "fragile spam-debounce" feedback.
- **feat(acl): in-container broker for the ACL decision CLI verbs**
  (#279, operator-greenlit Q5 / lead FUTURE item 4). Today an
  in-container ``sac a2a {unblock,block,grant}`` used to write the
  per-container state.db — silently ineffective against the host
  listen's ACL checks (which consult the HOST'S state.db, a
  different file). This PR adds three new host-listen routes
  (``POST /v1/acl/{unblock,block,grant}``) + a stdlib-only HTTP
  broker (``_state/_acl_broker_client.py``) that mirrors the
  SAC-from-SAC ``_spawn_client`` pattern from #261. The CLI
  detects in-SIF via ``_lifecycle._in_sif_broker.is_in_sif`` and
  routes accordingly: in-SIF → host listen HTTP, bare-host →
  local DB helpers directly. Receivers can run the verb from any
  context and the write lands on the right db.

## [0.21.7] — 2026-06-01

### Fixed
- **fix(build): pin `hatchling<1.28` to dodge twine
  Metadata-Version 2.5 rejection.** hatchling ≥1.28 emits
  ``Metadata-Version: 2.5`` which the pypa-publish action's bundled
  twine rejects with ``InvalidDistribution: '2.5' is not a valid
  metadata version`` (verified on the v0.21.6 release build,
  workflow run 26728715926 — pypi-publish failed; no artifact landed
  on PyPI and no GitHub Release was created). Hatchling 1.27 emits
  ``Metadata-Version: 2.4`` which twine accepts. Same code contents
  as the unpublished v0.21.6 (see ``[0.21.6]`` section below for the
  feature/fix list); re-evaluate the pin when
  ``pypa/gh-action-pypi-publish`` ships a newer twine.

## [0.21.6] — 2026-06-01

### Added
- **feat(agents): `sac agents forget <name>`** (#270, operator
  backlog #3). New local-only registry-reset recovery verb for the
  "agent is gone, only stale rows persist" case (SLURM-reclaimed
  node, crashed peer that came back fresh, etc.). Tombstones the
  ``instances`` row with ``exit_reason='operator-forget'`` and
  unregisters the ``comms_nodes`` pin. NO ssh, NO local process
  signal. Refuses to act on a live instance unless ``--force`` is
  passed. Idempotent: no rows = no-op exit 0.

### Fixed
- **fix(start): preserve apptainer stderr in SIF build failures**
  (#271, operator backlog #4 partial). Pre-fix shape:
  ``_build_sif_from_{uri,def}`` returned ``False`` on apptainer
  build failure, silently dropping the stderr — callers saw only a
  generic "Failed to start agent" upstream with no diagnostic.
  Now ``capture_output=True`` + raises ``RuntimeError`` with the
  apptainer stderr verbatim. Success path unchanged.
- **fix(tests): drop unused `time` / `typing.Iterator` imports**
  (#273, ruff F401 cleanup on develop).
- **fix(tests): replace `monkeypatch` with `subprocess_shim` in
  test__forget** (#274, PA-306 §3 no-mocks). The new ``forget``
  test used ``monkeypatch.setattr`` which the audit gate
  forbids; refactored to the project's standard no-mocks
  ``subprocess_shim`` fixture (real fake ssh on $PATH).
- **fix(tests): drop literal `# noqa` from comment in
  test__handlers.py** (#275, ruff invalid-directive warning
  cleanup).

### Docs
- **docs(readme): refresh to v0.21.5** (#272). Adds ``sac agents
  forget``, the SAC-from-SAC broker subsection, the
  ``sac-listen.service`` systemd unit install recipe, the
  ``sac dev {systemd,cron,daemon}`` group, and ``sac registry
  sync``. Timestamp bumped.

## [0.21.5] — 2026-06-01

### Added
- **feat(listen): single-instance flock guard for `sac listen`** (#266,
  operator task #26 sub (1)). A second `sac listen` while one already
  holds the port used to crash uvicorn with bare `EADDRINUSE` + a
  Python traceback — loud but with no diagnostic about which process
  held the port. New `_listen/_single_instance.py` (`acquire_listen_lock`,
  `release_listen_lock`, `ListenAlreadyRunningError`, `default_lock_dir`)
  takes a port-scoped flock at `<lock_dir>/listen-<port>.pid` BEFORE
  uvicorn binds, stamps the current PID in the file body, and on
  conflict fails loud with the holding PID and lock-file path so
  `kill <pid>` is actionable without `lsof` / `netstat`. The flock is
  kernel-released on process exit (even SIGKILL / OOM) so a crashed
  listen never permanently jams the port. Acquired before
  `_register_self_comms_node` / `_maybe_sync_on_start` so a duplicate
  launch never touches the federated registry.
- **feat(systemd): `sac-listen.service` hand-maintained user unit**
  (#268, operator task #26 sub (3)). New `scripts/systemd/sac-listen.
  service` (`Type=simple` + `Restart=on-failure` + `RestartSec=5s` +
  `StandardOutput=journal`). The companion flock guard (sub (1))
  ensures `Restart=on-failure` cannot double-bind. The README is
  split into "federated scheduled jobs" (`sac.accounts-refresh`
  pattern, materialised from `scitex_dev.jobs`) vs "hand-maintained
  long-running services" (`sac-listen.service` lives here) so a
  future operator does not move this into the federated path by
  mistake. Install: copy to `~/.config/systemd/user/`,
  `daemon-reload`, `enable --now`.

### Tests
- **test(channel): pin SSE auto-reconnect across listen restart**
  (#267, operator task #26 sub (2)). Three no-mocks regression tests
  in `tests/scitex_agent_container/_mcp/test_channel_reconnect.py`
  using a real asyncio TCP server pin the invariant that the
  in-container SSE consumer (`_mcp/channel.py::_consume_sse`)
  reconnects after the listen drops the stream mid-flight (operator-
  restart scenario), records ≥ 2 actual TCP connection attempts, and
  recovers when the server starts AFTER the consumer is already
  trying. The exponential-backoff loop (0.5s → 30s cap) was already
  implemented; this PR pins it against regression. A flake-guard
  using a held-socket port reservation replaces a brittle "start →
  stop → restart on same port" approach.

## [0.21.4] — 2026-06-01

### Added
- **feat(broker): SAC-from-SAC — in-SIF `agent_start` brokers to host
  `sac listen`** (#261, operator-mandated 2026-06-01). When an agent
  runs INSIDE an apptainer SIF, `sac agents start <child>` (and the
  `agent_start` API) auto-detects the in-SIF condition
  (`APPTAINER_CONTAINER` / `SINGULARITY_CONTAINER`) and POSTs the
  spawn RPC to the host-side `sac listen` instead of trying nested
  apptainer (which is unsupported on the target HPC shape). The host
  re-runs `check_spawn`, records the parent → child lineage edge, and
  shells the real `sac agent start` against the bare host's
  apptainer. New `_lifecycle/_in_sif_broker.py`
  (`is_in_sif`, `broker_start_to_host`, `maybe_broker_in_sif_spawn`,
  `InSifBrokerError`); injection seam `in_sif_opener` on
  `agent_start`. Reuses the existing `SAC_LISTEN_BASE_URL` /
  `SAC_LISTEN_BEARER` env injection from
  `runtimes/_apptainer_listen_env.listen_env_flags`. Fail-loud
  contract: missing base URL / transport / 4xx / 5xx / malformed
  body → `InSifBrokerError` with status + body preserved verbatim.
  Bare-host path unchanged. Also fixes a latent listen-side bug
  exposed by the live test: `_listen/_agent_exec.py::agents_start`
  used to shell `["sac", "agent", "start", name]` (singular, removed
  in F-CS13); switched to `["sac", "agents", "start", name]` with a
  regression test pinning the argv shape.
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

### Fixed
- **fix(creds): :rw bind the per-account snapshot directly — no
  boot-time copy** (#262, operator task #15). Root cause of the
  2026-06-01 fleet-wide silent 401 outage: agents pinned via
  `spec.claude.account` got a FROZEN BOOT-COPY of the saved account's
  snapshot under `<state_dir>/claude/.credentials.json`. The
  in-container Claude CLI's ~1h OAuth refresh wrote back to that
  per-agent copy — not the source snapshot. After ~8h drift, every
  SDK turn 401'd silently (the telegram bridge still marked inbound
  👀 but the agent could not complete a turn). Hit hub, orochi, and
  proj-scitex-agent-container (revived only by restart). Fix:
  `runtimes/_apptainer_creds.resolve_cred_file` pinned branch now
  returns the snapshot path directly. The caller's existing `:rw`
  bind in `_apptainer_auth.auth_argv` lands on the snapshot — the
  in-container CLI's refresh writeback goes to the snapshot, which
  is now self-healing and never expires while any pinned agent keeps
  running. Same-account-pinned agents now share a single mount
  target; the Claude CLI's atomic refresh writeback (tmp+rename) is
  safe under concurrent refresh. Safety gates (`PinnedAccountError`
  on absent / missing-`expiresAt` / already-expired snapshot)
  preserved verbatim. Operators upgrading mid-deploy with a leftover
  `<state_dir>/claude/.credentials.json` are unaffected (resolver
  neither reads nor mutates the legacy dest; regression test in
  place).
- **fix(tests): satisfy STX-TQ002 AAA-marker rule on safety-gate
  raises tests** (#263). Follow-up cleanup of an audit-gate violation
  that slipped onto develop when #262 was auto-merged before
  pytest-matrix completed. Three safety-gate tests used a combined
  `# Act / Assert` comment; split into `# Act` + `# Assert —
  pytest.raises is the assertion` (matches the sibling
  `test__apptainer_creds.py` style). No production code change.

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
- **chore(tests): package-level `conftest.py` clears in-SIF env
  pollution.** Two autouse fixtures clear `APPTAINER_CONTAINER` /
  `SINGULARITY_CONTAINER` (would route every test through the new
  SAC-from-SAC broker) and `SCITEX_AGENT_CONTAINER_AGENT` / `SAC_AGENT`
  (leaks the running agent's identity into statusline tests).
  Side-effect: fixes 8 pre-existing in-SIF env failures that surfaced
  only when pytest runs inside an agent SIF.

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
