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

Default (host-live) path:

```
--bind <cred_file>:/tmp/sac-claude/.credentials.json:rw
--env  CLAUDE_CONFIG_DIR=/tmp/sac-claude
```

The **`:rw`** is why live refresh works: when the bundled `claude`
inside the container detects a near-expiry access token (~5 min skew),
it writes the new token back through the bind to the host canonical.
Without `:rw` the container 401s the moment the token expires.

Target lives under `/tmp/` (not `$HOME`) because D2 hardened preflight
requires `$HOME` empty; `CLAUDE_CONFIG_DIR=/tmp/sac-claude` keeps SDK and
CLI in sync without polluting `$HOME`.

### Per-account COPY caveat — **write-back is lost**

When `spec.claude.account` is non-empty,
`runtimes/_apptainer_creds.resolve_cred_file` **`shutil.copy2`**s the
store snapshot into the agent's own state dir and returns that path; the
caller binds **the copy** `:rw`. So:

- The container's refresh writes into the **agent-local copy**.
- The host canonical never advances.
- The agent stays alive until the frozen copy's refresh token dies, then
  401s with no host-visible recovery.

**Implication for a refresher agent:** do **not** use
`spec.claude.account`. Bind the host canonical directly:

```yaml
apptainer:
  binds:
    - "/home/<user>/.claude/.credentials-<account>.json:/tmp/sac-claude-<account>/.credentials.json:rw"
env:
  CLAUDE_CONFIG_DIR: "/tmp/sac-claude-<account>"
# leave spec.claude.account UNSET
```

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
expiry **while a session is active**. With the `:rw` bind the new token
persists to the host canonical; every other agent and refresher sharing
that canonical sees the fresh token on its next read.

So the canonical stays fresh **as long as something is talking to the
account**. Standard pattern:

- **One per-account refresher agent** on the cheapest model
  (`claude-haiku-4-5`), direct-bound to the canonical with a custom
  `CLAUDE_CONFIG_DIR` (see YAML snippet above).
- **A 30-minute keepalive cron** that posts an A2A turn to each
  refresher: `sac agents send <refresher> hello`. The turn forces the
  bundled CLI to spin a session, observe the impending expiry, and
  rotate the access token through the bind back to the canonical.

Every other agent on the same account benefits transparently — they all
bind the same canonical.

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

Operator splits CI auth across N Anthropic Max accounts to load-balance the
per-account 429 rate. Lead 2026-05-29 example: 66 `scitex-*` packages divided
22-22-22 across `wyusuuke-gmail-com`, `ywata1989-gmail-com`,
`ywatanabe-scitex-ai`.

Recipe:

```bash
for acct in wyusuuke-gmail-com ywata1989-gmail-com ywatanabe-scitex-ai; do
    SRC=/home/ywatanabe/.scitex/agent-container/accounts/$acct/.credentials.json
    scitex-dev creds rotate-all --source "$SRC" --only pkg1 --only pkg2 ... --yes
done
```

`--only` is repeatable; pass each package once per account.

Non-negotiables:

- **Silent-no-op trap.** If `--source` points to a file whose
  `claudeAiOauth.expiresAt` is in the past, `rotate-all` exits 0 with **zero
  stdout** (not error, not warning). Looks like success, does nothing. Always
  verify expiry before invoking.
- **Two locations, only one valid for `--source`:**
  - **Sac-store path — USE THIS:** `~/.scitex/agent-container/accounts/<acct>/.credentials.json`,
    kept fresh by the watch-live daemon (§6); what `sac accounts list` reads.
  - **Stale standalone copies — DO NOT USE:** `~/.claude/.credentials-<acct>.json`,
    written once at account-add time, never refreshed, expire in ~8 h. These
    are the canonicals §1 describes — they receive write-back via the `:rw`
    bind of a running agent, not via direct rotation.

Diagnosis:

```bash
jq '.claudeAiOauth.expiresAt' <path>   # epoch ms
echo $(($(date +%s) * 1000))           # now in ms
```

Or `sac accounts list` reports freshness for every account in the sac store.

## 6. The watch-live daemon — what keeps the sac store fresh

`sac accounts watch-live` runs `inotifywait -m` on `~/.claude/` for
`close_write|moved_to|create` events on `.credentials.json`. On every event it
atomically copies the live credential to the matching sac-store path; the slug
map turns the account email into a store name (e.g. `wyusuuke@gmail.com` →
`wyusuuke-gmail-com`).

```bash
sac accounts watch-live    # long-running; foreground or explicit & / supervisor
```

Non-negotiables:

- **NOT auto-started.** No systemd / launchd unit ships by default. If the
  daemon isn't running when `claude /login` updates the live cred, the sac
  store stays stale until the operator manually runs `sac accounts sync-live`
  or starts the daemon.
- **Manual one-shot fallback:** `sac accounts sync-live` runs the same atomic
  copy once without the daemon — use after a `claude /login` performed with
  the daemon stopped, or to catch up before a rotation.

Implementation:

- `src/scitex_agent_container/_account/creds_watch.py` L140-152
  (`watch_inotify()`), L99-133 (`watch_poll()` polling fallback for hosts
  without `inotifywait`), L35 (imports + calls into `sync_live()`).
- `src/scitex_agent_container/_account/creds_sync.py` L141-244 (`sync_live()`:
  atomic copy + freshness check), L69-77 (`slugify_email()`).
- `src/scitex_agent_container/cli_pkg/_account_sync_live.py` L82-100+
  (`account_watch_live` click command — daemon entry point).

## See also

- [27_credentials-relogin.md](27_credentials-relogin.md) — verified
  re-login flow (tmux code-paste) + 401-recovery design.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is the wrong place for credentials; `setting_sources=[]`.
- Source files cited: `runtimes/_sdk_common.py`,
  `runtimes/_apptainer_auth.py`, `runtimes/_apptainer_creds.py`,
  `_state/_preflight_creds.py`.
