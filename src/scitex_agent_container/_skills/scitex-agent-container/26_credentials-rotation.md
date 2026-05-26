---
description: |
  [TOPIC] scitex-agent-container — SAC OAuth credentials rotation / refresh / recovery for Claude Pro/Max accounts (verified ywata-note-win 2026-05-26).
  [DETAILS] Per-account `~/.claude/.credentials-<account>.json` canonicals (mode 0600) with `.credentials.json` and all other references as symlinks; the `:rw` bind at `/tmp/sac-claude/.credentials.json` that lets the bundled `claude` refresh access tokens in place; the per-account COPY in `_apptainer_creds.resolve_cred_file` that LOSES write-back when `spec.claude.account` is set (a refresher must bind the canonical directly); the preflight (`_state/_preflight_creds`, 300s skew) that only checks access-token expiry and is skipped under an API-key env, so a dead bound file starts then 401s LOUD on turn 1; the per-account refresher + 30-min keepalive cron; and the verified headless re-login flow (code-paste via tmux, isolated `CLAUDE_CONFIG_DIR`, `/bin/cp -f` promote, `sac agents restart --yes`).
tags: [scitex-agent-container-credentials-rotation]
---

# SAC OAuth credentials: rotation, refresh, and recovery

> Verified on `ywata-note-win` 2026-05-26.

Operator runbook for the **Anthropic OAuth credentials** SAC binds into
every Claude-backed agent: the on-disk model, the mechanics behind live
refresh, why some configs silently lose write-back, what the preflight
catches, and the verified headless re-login flow. It does **not** cover
the API-key (`SAC_ANTHROPIC_API_KEY`) path — the preflight
short-circuits on any API-key env
(`_state/_preflight_creds._api_key_env_is_set`).

## 1. Model — one canonical per account, everything else a symlink

Each account has exactly one real file (mode **0600**):

```
~/.claude/.credentials-<account>.json     # e.g. ywatanabe / scitex-ai / spartan
```

`~/.claude/.credentials.json` (the "active" file) is **always a symlink** to whichever canonical the host is logged in as; every other reference (per-agent state dirs, lead pointers) is **also a symlink** to the same canonical. Never a second real copy.

Two non-negotiables:

- **Never copy a credentials file into `to_home/`** — it is git-tracked (committing a token leaks it), and the symlink-resolver dereference-copies a **snapshot**, losing write-back (see [25_claude-setup-delivery.md](25_claude-setup-delivery.md)).
- **Never bake credentials into the SIF image** — shared across accounts/machines, so it is wrong for N-1 and unrotatable.

The canonical is the only thing the bundled `claude` may write back to.

## 2. Auth mechanics — canonical → SDK, kept fresh

Three cooperating pieces (citations are the SSoT):

**`provision_anthropic_auth` (`runtimes/_sdk_common.py`)** — precedence:

1. Pop any bare `ANTHROPIC_API_KEY` unconditionally (a stale dotfiles
   export must not survive).
2. Resolve via `_cred_file_path()`; if the file exists, **run the
   preflight** (`check_oauth_token_expiry`) and return
   `"credentials_file"`. **No env mirroring** — Anthropic rejects
   `sk-ant-oat*` as a bare env, so the SDK reads the file directly.
3. Else if `SAC_ANTHROPIC_API_KEY` set, mirror to `ANTHROPIC_API_KEY`,
   return `"sac_env"`.
4. Else raise `SDKCommonError`.

**`_cred_file_path` (`runtimes/_sdk_common.py`)** — `CLAUDE_CONFIG_DIR`
if set → `<dir>/.credentials.json`, else `~/.claude/.credentials.json`.
Same env the bundled `claude` respects, so SDK, CLI, and helper agree.

**Apptainer bind (`runtimes/_apptainer_auth.py::auth_argv`)** — default
(host-live) path:

```
--bind <cred_file>:/tmp/sac-claude/.credentials.json:rw
--env  CLAUDE_CONFIG_DIR=/tmp/sac-claude
```

The **`:rw`** is why live refresh works: the in-container `claude`
writes the renewed token back through the bind to the host canonical.
Target is under `/tmp/` (not `$HOME`) because the D2 hardened preflight
requires `$HOME` empty.

### Per-account COPY caveat (`runtimes/_apptainer_creds.py::resolve_cred_file`) — write-back is LOST

When `spec.claude.account` is set, `resolve_cred_file` `shutil.copy2`s
the store snapshot into the agent's state dir and binds **the copy**
`:rw`. So refresh writes into the **agent-local copy**, the host
canonical never advances, and the agent 401s once its frozen copy's
refresh token dies — with no host-visible recovery.

**So a refresher agent must NOT use `spec.claude.account`.** Bind the
host canonical directly so refresh **writes through**:

```yaml
apptainer:
  binds:
    - "/home/<user>/.claude/.credentials-<account>.json:/tmp/sac-claude-<account>/.credentials.json:rw"
env:
  CLAUDE_CONFIG_DIR: "/tmp/sac-claude-<account>"
# leave spec.claude.account UNSET
```

### Preflight (`_state/_preflight_creds.check_oauth_token_expiry`) — what it does NOT do

`EXPIRY_SKEW_SECONDS = 300`. Reads
`data["claudeAiOauth"]["expiresAt"]` (ms or s, auto-detected via
`> 1e12`) and refuses to start if the **access** token is within 300 s
of expiry. **Skipped** when any API-key env is set. It does **not**
inspect the refresh token and makes **no network call**. So:

> A direct-bind agent **starts** (if access has >5 min left) then fails
> **LOUD with 401 on turn 1** if the bound file is actually dead
> (refresh token expired, account revoked). The first 401 is the
> authoritative signal that the canonical needs operator recovery.

## 3. Auto-refresh — the CLI does it; sac just keeps a session live

The bundled `claude` renews the access token in place ~5 min before
expiry **while a session is active**; with `:rw` the new token persists
to the host canonical and every agent sharing it sees the refresh on
next read. So the canonical stays fresh **as long as something talks to
the account**. Pattern:

- **One per-account refresher agent** on the cheapest model
  (`claude-haiku-4-5`), direct-bound to the canonical (YAML above).
- **A 30-min keepalive cron**: `sac agents send <refresher> hello` —
  forces a session, which observes impending expiry and rotates the
  token through the bind.

## 4. Expired ≠ unrecoverable — until the refresh token dies

The JSON carries an `accessToken` (~8 h) and a `refreshToken` (hours to
days). The CLI swaps an expired access token using the refresh token:

- **Access expired, refresh alive** → CLI rotates silently.
- **Access + refresh both expired** → CLI cannot self-revive; next
  session 401s. **Re-login required** (§5). (Verified: `scitex-ai` hit
  an expired refresh; §5 cleared it.)

## 5. Verified re-login flow — headless, operator-in-loop

Bundled CLI auth subcommands: `claude auth login` / `logout` / `status`.
`login` is **not** a localhost redirect — `redirect_uri` is
`https://platform.claude.com/oauth/code/callback`, which shows a
**code** of the form `<code>#<state>` to paste back. So the operator
needs a direct-injection return path (`tmux send-keys` or an A2A turn).

1. **Back up the live canonical** first:
   ```
   /bin/cp -f ~/.claude/.credentials.json ~/.claude/.credentials.json.bak
   ```
2. **Login in an isolated `CLAUDE_CONFIG_DIR` via tmux** (host
   `~/.claude/` untouched):
   ```
   tmux new -d -s login-<acct> "CLAUDE_CONFIG_DIR=/tmp/login-<acct> claude auth login"
   ```
3. **Capture the pane and reassemble the URL** — it **wraps across pane
   lines** (no inserted spaces); join the fragments, don't rely on a
   single-line grep.
4. **Surface only the URL** + which account (so the operator picks the
   right one). Never print token values; only `expiresAt` /
   `subscriptionType` are safe to log.
5. **Operator authorizes**; `platform.claude.com` shows `<code>#<state>`
   (single-use, never log).
6. **Inject the full `<code>#<state>`** at `Paste code here >`:
   ```
   tmux send-keys -t login-<acct> "<code>#<state>" Enter
   ```
   CLI prints `Login successful`, writes
   `/tmp/login-<acct>/.credentials.json` (`expiresAt` ~+8 h).
7. **Promote to the canonical** (`/bin/cp` bypasses aliased `cp` — §7):
   ```
   /bin/cp -f /tmp/login-<acct>/.credentials.json ~/.claude/.credentials-<acct>.json
   chmod 600 ~/.claude/.credentials-<acct>.json
   ```
   If `<acct>` is active, re-point the symlink:
   ```
   ln -sfn ~/.claude/.credentials-<acct>.json ~/.claude/.credentials.json
   ```
8. **Restart the refresher** (`--yes` mandatory — §7):
   ```
   sac agents restart cred-refresher-<acct> --yes
   ```
9. **Verify**: `sac agents send cred-refresher-<acct> hello` → expect a
   plain reply, not a 401.

## 6. 401-recovery layer (design)

Hook the refresher/cron so a 401 auto-runs §5: spawn login in an isolated `CLAUDE_CONFIG_DIR`, reassemble the wrapped URL, notify the operator with `{machine, host, agent, account, URL}` (typically via the Telegram MCP tools — [23_telegram-integration.md](23_telegram-integration.md)), paste the returned `<code>#<state>` via `tmux send-keys`/A2A, promote (§5.7) and restart (§5.8). The whole account's fleet recovers off the shared canonical. Because it is code-paste, plan a direct-injection return path from day one.

## 7. Gotchas

- **`/bin/cp -f` (not `cp -f`).** A shell `cp` alias commonly maps to
  `cp -i`, which prompts and silently no-ops non-interactively.
- **`sac agents restart` needs `--yes`** or it hangs on the prompt.
- **The URL wraps across tmux pane lines** — join fragments; a naive
  `grep '^https://'` sees only the first.
- **The pasted value is `<code>#<state>`** — the `#` and `<state>` are
  part of the payload, not a comment.
- **Never print token values.** Log only `expiresAt` /
  `subscriptionType`.
- **Always log in inside an isolated `CLAUDE_CONFIG_DIR`** and back up
  the canonical first — never disturb the lead/host session.

## See also

- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is wrong for credentials; `setting_sources=[]` isolation.
- [40_troubleshooting.md](40_troubleshooting.md) — generic 401 / start
  failure debugging.
- Sources: `runtimes/_sdk_common.py` (`provision_anthropic_auth`, `_cred_file_path`); `runtimes/_apptainer_auth.py` (`auth_argv` — `:rw` bind + `CLAUDE_CONFIG_DIR`); `runtimes/_apptainer_creds.py` (`resolve_cred_file` — per-account COPY); `_state/_preflight_creds.py` (`check_oauth_token_expiry`, `EXPIRY_SKEW_SECONDS=300`).
