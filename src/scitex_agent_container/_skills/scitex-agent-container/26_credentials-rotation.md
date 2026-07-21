---
description: |
  [TOPIC] scitex-agent-container — SAC OAuth credentials on-disk model + how the SDK reaches them inside the container.
  [DETAILS] Per-account `~/.claude/.credentials-<acct>.json` canonicals + mode-0600 symlinks; the `:rw` dir-bind at `/tmp/sac-claude/.credentials.json` that lets the bundled CLI refresh access tokens in place (~5 min skew); the per-account snapshot bind (post-PR #262 — the COPY caveat is fixed); the preflight (300s skew, access-token only); access vs refresh token life stages. Refresh mechanics + host-side ops live in [26_credentials-rotation-host.md](26_credentials-rotation-host.md). Re-login flow in 27_credentials-relogin.md.
tags: [scitex-agent-container-credentials-rotation]
---

# SAC OAuth credentials: model + auth mechanics

> Verified on `ywata-note-win` 2026-05-26.
> Refresh + host-side ops (auto-refresh, the one-account-one-refresher
> invariant, `sac.accounts-refresh` cron, `watch-live` daemon, CI
> rotation): [26_credentials-rotation-host.md](26_credentials-rotation-host.md).
> Re-login (after refresh-token death):
> [27_credentials-relogin.md](27_credentials-relogin.md).

Operator runbook for the **Anthropic OAuth credentials** SAC binds into
every Claude-backed agent. Covers the on-disk model, the auth mechanics
that make live refresh work, and what the preflight catches.

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

The **directory bind** (not a single-file bind) is required because
host-side refreshes rotate atomically via `write-tmp + rename`
(`_account/creds_watch.py`, `_account/creds_sync.py`,
`_account/claude_usage._refresh_access_token_at`). A single-file bind
would survive the rename pointing at the old inode — visible as
`...credentials.json//deleted` in `/proc/<pid>/mountinfo` — and the
container would read the stale pre-rename token forever → 401 at
natural expiry. See [ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 1" for the 2026-06-04 03:00 fleet-wide storm empirical
anchor.

Scope of the unpinned dir-bind: `~/.claude/` also contains
settings.json, projects DB, chat history, MCP config — the dir-bind
exposes (and now allows writes to) these. Recommended deployment is
`spec.claude.account` pinning + the watch-live daemon
([26_credentials-rotation-host.md §5](26_credentials-rotation-host.md))
so the unpinned dir-bind is a degraded fallback only.

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
- Directory bind, not single-file: same atomic-rename reason as above.

PR #262 (commits `d70e608` + `acf2cbb`) eliminated an earlier
`shutil.copy2` snapshot-into-agent-state-dir path that caused the
2026-06-01 silent outage. See [ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 1" for the historical story.

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

## 3. Expired ≠ unrecoverable — until the refresh token dies

Credentials JSON carries `accessToken` (~8 h life) and `refreshToken`
(multi-hour to multi-day). The bundled CLI quietly swaps an expired
access token for a new one using the refresh token.

Stages of death:

- **Access expired, refresh alive** → CLI rotates silently; no operator
  action.
- **Access expired, refresh expired** → CLI cannot self-revive; the next
  session 401s. **Re-login required** — see
  [27_credentials-relogin.md](27_credentials-relogin.md).

Refresh mechanics, the one-account-one-refresher invariant, and host-side
ops (cron, watch-live daemon, CI rotation) are in
[26_credentials-rotation-host.md](26_credentials-rotation-host.md).

## See also

- [26_credentials-account-selection.md](26_credentials-account-selection.md) —
  which account an agent boots on (`claude.account` pin vs the boot
  picker) and the `SAC_CREDS_7D_POLICY: spread | burn` spend policy.

- [26_credentials-rotation-host.md](26_credentials-rotation-host.md) —
  refresh mechanics + one-refresher invariant + host cron / watch-live /
  CI rotation.
- [ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md) —
  canonical write-up of the rotation model + the two failure modes
  (stale-COPY pre-#262, refresh-token race pre-#299).
- [27_credentials-relogin.md](27_credentials-relogin.md) — re-login
  (tmux code-paste) + 401-recovery design.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is the wrong place for credentials; `setting_sources=[]`.
- Source files cited: `runtimes/_sdk_common.py`,
  `runtimes/_apptainer_auth.py`, `runtimes/_apptainer_creds.py`,
  `_state/_preflight_creds.py`.
