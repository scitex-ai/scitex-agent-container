---
description: |
  [TOPIC] scitex-agent-container — SAC OAuth credentials rotation / refresh model for Claude Pro/Max accounts.
  [DETAILS] Per-account `~/.claude/.credentials-<acct>.json` canonicals + mode-0600 symlinks; the `:rw` bind at `/tmp/sac-claude/.credentials.json` that lets the bundled CLI refresh access tokens in place (~5 min skew); the per-account COPY caveat in `_apptainer_creds.resolve_cred_file` that LOSES write-back when `spec.claude.account` is set; the preflight (300s skew, access-token only); per-account refresher agent + 30-min keepalive cron. Re-login flow lives in 27_credentials-relogin.md.
tags: [scitex-agent-container-credentials-rotation]
---

# SAC OAuth credentials: model + auto-refresh

> Verified on `ywata-note-win` 2026-05-26.
> Re-login flow (operator-in-loop, after refresh-token death):
> [27_credentials-relogin.md](27_credentials-relogin.md).

Operator runbook for the **Anthropic OAuth credentials** SAC binds into
every Claude-backed agent. Covers the on-disk model, the auth mechanics
that make live refresh work, why some configurations silently lose
write-back, and what the preflight catches.

Does **not** cover the API-key (`SAC_ANTHROPIC_API_KEY`) path — the
preflight short-circuits on any API-key env (see
`_state/_preflight_creds._api_key_env_is_set`).

## 1. Model — one canonical per account, everything else symlinks

Each Anthropic account has exactly one real file:

```
~/.claude/.credentials-<account>.json     # one per account
```

Mode **0600** on the canonical and every symlink. The active file
`~/.claude/.credentials.json` is **always a symlink** to whichever
canonical the host is currently logged in as; per-agent state dirs and
lead session pointers are **also symlinks** to the same canonical. There
is never a second real copy on disk.

Non-negotiables:

- **Never copy a credentials file into `to_home/`.** It's git-tracked
  (see [25_claude-setup-delivery.md](25_claude-setup-delivery.md)) — a
  commit leaks the token; the symlink-resolver dereferences and lands a
  snapshot that loses write-back.
- **Never bake credentials into the SIF image.** The image is shared
  across accounts; baked creds are wrong for N-1 and unrotatable in any.

## 2. Auth mechanics — canonical → SDK, stays fresh

Three pieces in this repo cooperate:

### `provision_anthropic_auth` (`runtimes/_sdk_common.py`)

1. Pop any bare `ANTHROPIC_API_KEY` from env unconditionally — a stale
   dotfiles export must not survive.
2. Resolve the credentials path via `_cred_file_path()`; if the file
   exists, call the preflight and return `"credentials_file"`. **No env
   mirroring** — Anthropic rejects `sk-ant-oat*` OAuth tokens as bare env.
3. Else, if `SAC_ANTHROPIC_API_KEY` is set, mirror it into
   `ANTHROPIC_API_KEY` and return `"sac_env"`.
4. Else, raise `SDKCommonError`.

### `_cred_file_path` (`runtimes/_sdk_common.py`)

```
CLAUDE_CONFIG_DIR if set → <CLAUDE_CONFIG_DIR>/.credentials.json
else                     → ~/.claude/.credentials.json
```

Same env as the bundled `claude` CLI, so SDK + CLI + helper all resolve
to the same file.

### Apptainer bind (`runtimes/_apptainer_auth.py::auth_argv`)

Both paths (host-live AND pinned-account) dir-bind unconditionally
(post task #13):

```
--bind <cred_file_parent>:/tmp/sac-claude:rw
--env  CLAUDE_CONFIG_DIR=/tmp/sac-claude
```

* `cred_file_parent` = `~/.claude/` for unpinned / host-live (the
  agent picks up whatever account the operator is currently logged
  in as).
* `cred_file_parent` = `~/.scitex/agent-container/accounts/<acct>/`
  for pinned (the snapshot dir for the named account).

The **`:rw`** is why live refresh works: when the bundled `claude`
inside the container detects a near-expiry access token (~5 min skew),
it writes the new token back through the bind to the host canonical.
Without `:rw` the container 401s the moment the token expires.

The **directory bind** (not a single-file bind) is what keeps the
container's view in sync with host-side atomic-rename refreshes
(`_account/creds_watch.py` watch-live mirror, `_account/creds_sync.py
_atomic_copy`, `_account/claude_usage._refresh_access_token_at`).
A single-file bind would survive the rename pointing at the old inode
— visible as `...credentials.json//deleted` in
`/proc/<pid>/mountinfo` — and the container reads the stale
pre-rename token forever → 401 at natural expiry. See
[ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 1" for the empirical anchor (2026-06-04 03:00
fleet-wide storm).

Scope acknowledgement for the unpinned dir-bind: `~/.claude/` contains
more than just `.credentials.json` (settings.json, projects DB, chat
history, MCP config). The dir-bind exposes these to the container,
where the previous file-bind exposed only the creds file. The
bundled in-container CLI already READS these via `CLAUDE_CONFIG_DIR`;
the change is that it can now also WRITE to them. The recommended
deployment is `spec.claude.account` pinning + the watch-live daemon
(§ 6) mirroring the host-active account into the snapshot store, so
the unpinned dir-bind is a degraded fallback for the host-active-login
case only.

Target lives under `/tmp/` (not `$HOME`) because D2 hardened preflight
requires `$HOME` empty; `CLAUDE_CONFIG_DIR=/tmp/sac-claude` keeps SDK and
CLI in sync without polluting `$HOME`.

### Per-account bind — **live snapshot, not a copy** (post-PR #262)

When `spec.claude.account` is non-empty,
`runtimes/_apptainer_creds.resolve_cred_file` returns the **snapshot
path itself** (`~/.scitex/agent-container/accounts/<acct>/.credentials.json`).
`runtimes/_apptainer_auth.auth_argv` then binds the snapshot's **parent
directory** `:rw`:

```
--bind <snapshot_parent>:/tmp/sac-claude:rw
--env  CLAUDE_CONFIG_DIR=/tmp/sac-claude
```

So:

- The container's refresh writes through the bind to the **same
  snapshot file** every other reader looks at.
- The snapshot stays current. Every other agent on the same account
  sees the new tokens on its next read.
- A directory bind (not a single-file bind) is required because the
  bundled CLI rotates atomically via `write-tmp + rename` — a file
  bind would survive that rename pointing at the old inode.

**Historical caveat (pre-PR #262, NOW FIXED):** the runtime used to
`shutil.copy2` the snapshot into the agent's own state dir and bind
the copy. Refresh wrote to the agent-local copy; the snapshot never
advanced; once the copy's refresh-token died the agent 401'd silently.
That was the 2026-06-01 fleet-wide silent outage. PR #262 (commits
`d70e608` + `acf2cbb`) eliminated the copy path. See
[ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 1" for the full story.

### Preflight (`_state/_preflight_creds.check_oauth_token_expiry`)

`EXPIRY_SKEW_SECONDS = 300`. Reads `data["claudeAiOauth"]["expiresAt"]`
(ms or s, auto-detected via `> 1e12`) and refuses to start if the
**access** token is within 300 s of expiring or already dead. Skipped
entirely when any API-key env is set.

It does **not** inspect the refresh token. It does **not** make a
network call. So:

> A direct-bind agent **starts** (preflight passes if access still has
> more than five minutes of life) then fails **LOUD** at the **first
> turn** with 401 if the bound file is in fact dead (refresh token
> expired, account revoked).

The first 401 is the first authoritative signal that the canonical
needs operator recovery → see
[27_credentials-relogin.md](27_credentials-relogin.md).

## 3. Auto-refresh — the bundled CLI does it; sac keeps a session live

The bundled `claude` renews the **access** token in place ~5 min before
expiry **while a session is active**. With the `:rw` snapshot bind the
new token persists to the snapshot store; every other agent sharing
that snapshot sees the fresh token on its next read.

So the snapshot stays fresh **as long as something is talking to the
account**. Standard pattern for **parked** accounts (no running pinned
agent, not the host's interactive login):

- **One per-account refresher agent** on the cheapest model
  (`claude-haiku-4-5`), pinned via `spec.claude.account: <acct>`. Post-PR
  #262 the live snapshot bind is automatic — no manual bind / custom
  `CLAUDE_CONFIG_DIR` override needed.
- **A 30-minute keepalive cron** that posts an A2A turn to each
  refresher: `sac agents send <refresher> hello`. The turn forces the
  bundled CLI to spin a session, observe the impending expiry, and
  rotate the access token through the bind back to the snapshot.

Every other agent on the same account benefits transparently — they all
bind the same snapshot.

> Mechanics note: the bundled `claude` CLI caches the refresh-token
> **in memory** at session start; it does not re-read the snapshot
> before each rotation. This is what makes the one-account-one-refresher
> invariant load-bearing (§3a). Any out-of-band rotator (e.g., a host
> cron call against the same account) will invalidate the running
> CLI's in-memory refresh-token server-side and 401 the next turn.

## 3a. One account, one refresher — the rotation-race invariant

OAuth refresh-tokens **rotate server-side on every use**: each call to
`/oauth/token` returns a new refresh-token AND invalidates the previous
one with no grace window. Combined with the in-memory cache (§3), this
means **at most one process can be the live refresher for any given
account at any given time**:

* **Pinned-and-running**: the in-container `claude` CLI inside the
  pinned agent is the sole refresher. The host cron MUST skip the
  account.
* **Host's interactive login**: the operator's interactive `claude`
  session is the sole refresher. The host cron MUST skip it (the
  pre-existing `--skip-active` behaviour).
* **Parked** (no pinned agent, no interactive session): the host cron
  is the sole refresher. Safe to rotate; no in-memory cache to race.

If two refreshers ever touch the same account concurrently, the loser's
refresh-token is dead the moment the winner's `/oauth/token` returns.
The loser then 401s on its next turn — even though the snapshot file
on disk looks brand-new (the winner wrote it).

**The host refresher cron** (`sac.accounts-refresh.service`, see §5b)
implements this invariant via `--skip-active`: the skip-set is the union
of the host-active account and every account currently pinned by a
running local agent (PR #299, commit `dea298d`). Stale-registry
tolerance: a dead agent's leftover JSON over-skips its account
(under-refresh — safe direction; recover with manual
`sac accounts refresh <name>`). The opposite direction (over-refresh
racing a live agent) was the 2026-06-03 ~hourly 401 storm that
PR #299 closes structurally. See
[ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 2" + § "Why a symlink doesn't solve this".

## 4. Expired ≠ unrecoverable — until the refresh token dies

Credentials JSON carries `accessToken` (~8 h life) and `refreshToken`
(multi-hour to multi-day). The bundled CLI quietly swaps an expired
access token for a new one using the refresh token.

Stages of death:

- **Access expired, refresh alive** → CLI rotates silently; no operator
  action.
- **Access expired, refresh expired** → CLI cannot self-revive; the next
  session 401s. **Re-login required** — see
  [27_credentials-relogin.md](27_credentials-relogin.md).

## 5. Multi-account CI rotation

`for acct in ...; scitex-dev creds rotate-all --source $SAC_STORE --only pkgA --only pkgB ... --yes; done` splits CI auth across N Max accounts (lead 2026-05-29: 66 `scitex-*` divided 22-22-22 across 3). `--only` is repeatable.

Non-negotiables:

- **Silent-no-op trap.** Stale `--source` (`claudeAiOauth.expiresAt` in the past) exits **0 with zero stdout**. Verify first: `jq '.claudeAiOauth.expiresAt' <path>` vs `echo $(($(date +%s) * 1000))`.
- **`--source` MUST be the sac store**, `~/.scitex/agent-container/accounts/<acct>/.credentials.json` (kept fresh by §6's daemon; what `sac accounts list` reads). The `~/.claude/.credentials-<acct>.json` canonicals from §1 are refreshed via `:rw` agent binds, NOT direct rotation — using them as `--source` silently fails when stale.

## 5b. The host refresher cron — `sac.accounts-refresh`

A federated systemd-user timer fires `sac accounts refresh --all --skip-active` every 2h (`OnUnitActiveSec=2h`). It iterates the account store and rotates each unpinned account's tokens against `/oauth/token`. Registered as the `sac.accounts-refresh` JobSpec via `_jobs_plugin.py`; install with `sac dev systemd install --yes`.

The `--skip-active` skip-set is the union of:

1. **The host's interactive login** (`~/.claude/.credentials.json` resolved to a stored account name via `_resolve_active_account_name`).
2. **Every account currently pinned by a running local agent** (`_collect_pinned_running_accounts`, post-PR #299, commit `dea298d`).

Both subsets enforce the one-account-one-refresher invariant from §3a. Diagnostic stderr lines name each excluded account with the reason so the operator can see what got skipped:

```
[skip-active] excluding active account 'ywatanabe-gmail-com'.
[skip-active] excluding pinned-running account 'wyusuuke-gmail-com' (refresh-token rotation race guard).
[skip-active] excluding pinned-running account 'ywatanabe-scitex-ai' (refresh-token rotation race guard).
```

Implementation: `cli_pkg/_account_refresh.py::account_refresh` (CLI wiring), `_collect_pinned_running_accounts(home)` (reads `~/.scitex/agent-container/runtime/registry/*.json` directly to avoid the `Registry` class's import-time `REGISTRY_DIR` freeze under pytest fixtures).

## 6. The watch-live daemon — keeps the sac store fresh

`sac accounts watch-live` runs `inotifywait -m ~/.claude/` for `close_write|moved_to|create` on `.credentials.json`; atomically copies each event into the matching sac-store path (slug-map e.g. `wyusuuke@gmail.com` → `wyusuuke-gmail-com`).

Non-negotiables:

- **NOT auto-started** — no systemd / launchd unit ships. If the daemon isn't running when `claude /login` refreshes, the sac store stays stale until `sac accounts sync-live` (one-shot fallback) or the daemon starts.

Implementation: `_account/creds_watch.py` L140-152 (`watch_inotify()`), L99-133 (`watch_poll()` fallback); `_account/creds_sync.py` L141-244 (`sync_live()`), L69-77 (`slugify_email()`); `cli_pkg/_account_sync_live.py` L82-100+ (`account_watch_live` command).

## See also

- [ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md) —
  the canonical write-up of the rotation model + the two failure modes
  (stale-COPY pre-#262, refresh-token race pre-#299) + the
  one-account-one-refresher invariant + the symlink-doesn't-help
  argument. Read this if you're touching `_apptainer_creds.py`,
  `_apptainer_auth.py`, or `cli_pkg/_account_refresh.py`.
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified
  re-login flow (tmux code-paste) + 401-recovery design.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is the wrong place for credentials; `setting_sources=[]`.
- Source files cited: `runtimes/_sdk_common.py`,
  `runtimes/_apptainer_auth.py`, `runtimes/_apptainer_creds.py`,
  `_state/_preflight_creds.py`, `cli_pkg/_account_refresh.py`.
