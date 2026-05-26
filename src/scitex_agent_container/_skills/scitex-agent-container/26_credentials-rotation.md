---
description: |
  [TOPIC] scitex-agent-container — SAC OAuth credentials rotation / refresh / recovery for Claude Pro/Max accounts (verified ywata-note-win 2026-05-26).
  [DETAILS] Three per-account ~/.claude/.credentials-<account>.json as the single source of truth, with .credentials.json + every other reference as mode 0600 symlinks to a canonical; the rw bind at /tmp/sac-claude/.credentials.json that lets the bundled claude refresh access tokens in place (~5 min skew); the per-account COPY in _apptainer_creds.resolve_cred_file that LOSES write-back when spec.claude.account is set (refresher must instead bind the canonical via spec.apptainer.binds + custom CLAUDE_CONFIG_DIR); the preflight (_state/_preflight_creds.check_oauth_token_expiry, 300s skew) that only inspects access-token expiresAt and is skipped under an API-key env, so a direct-bind agent starts then fails LOUD with 401 the first turn if the bound file is dead; per-account refresher agent + 30-min keepalive cron pattern; and the verified headless re-login flow (code-paste via tmux, isolated CLAUDE_CONFIG_DIR, /bin/cp -f promote, sac agents restart --yes).
tags: [scitex-agent-container-credentials-rotation]
---

# SAC OAuth credentials: rotation, refresh, and recovery

> Verified on `ywata-note-win` 2026-05-26.

This skill is the operator runbook for the **Anthropic OAuth credentials**
SAC binds into every Claude-backed agent. It covers the on-disk model, the
auth mechanics that make live refresh work, why some configurations
silently lose write-back, what the preflight does (and does not) catch,
and the verified headless re-login flow when a refresh token has actually
died.

It does **not** cover the API-key (`SAC_ANTHROPIC_API_KEY`) path — that
path is unaffected by everything below because the preflight short-circuits
on any API-key env (see `_state/_preflight_creds._api_key_env_is_set`).

## 1. Model — three per-account files, one canonical, everything else is a symlink

**Single source of truth.** Each Anthropic account has exactly one
real on-disk credentials file:

```
~/.claude/.credentials-<account>.json     # account A: e.g. ywatanabe
~/.claude/.credentials-<account>.json     # account B: e.g. scitex-ai
~/.claude/.credentials-<account>.json     # account C: e.g. spartan
```

Mode is **0600** on the canonical and every symlink. The "active"
file `~/.claude/.credentials.json` is **always a symlink** to whichever
canonical the host is currently logged in as; any other reference
(per-agent state dirs, lead session pointers, etc.) is **also a symlink**
to the same canonical. There is never a second real copy on disk.

This rule is enforced by two non-negotiables:

- **Never copy a credentials file into `to_home/`.** `to_home/` is the
  git-tracked agent-definition tree (see
  [25_claude-setup-delivery.md](25_claude-setup-delivery.md)) — committing
  a token leaks it; and the symlink-resolver dereference-copies, which
  would land a **snapshot** of the canonical and lose write-back (an
  in-container refresh would write into the snapshot, not the host
  canonical, so the host stays dead).
- **Never bake credentials into the SIF image.** The image is shared
  across accounts and machines; a baked credential is wrong for at least
  N-1 of them and unrotatable in any of them.

The canonical is the only thing the bundled `claude` CLI is allowed to
write back to.

## 2. Auth mechanics — how the canonical reaches the SDK and stays fresh

Three pieces in this repo cooperate. Citations are the SSoT.

### `provision_anthropic_auth` (`runtimes/_sdk_common.py`)

Precedence (in order):

1. Pop any bare `ANTHROPIC_API_KEY` from env unconditionally
   (`os.environ.pop("ANTHROPIC_API_KEY", None)`) — a stale dotfiles export
   must not survive past this point.
2. Resolve the credentials path via `_cred_file_path()`; if the file
   exists, **call the preflight** (`check_oauth_token_expiry`) and on
   success return `"credentials_file"`. **No env mirroring** — Anthropic
   rejects `sk-ant-oat*` OAuth tokens passed as a bare env, so the SDK
   must read the file directly.
3. Else, if `SAC_ANTHROPIC_API_KEY` is set, mirror it into
   `ANTHROPIC_API_KEY` and return `"sac_env"`.
4. Else, raise `SDKCommonError`.

### `_cred_file_path` (`runtimes/_sdk_common.py`)

```
CLAUDE_CONFIG_DIR if set → <CLAUDE_CONFIG_DIR>/.credentials.json
else                     → ~/.claude/.credentials.json
```

This is the **same env** the bundled `claude` CLI itself respects, so the
SDK, the CLI, and the auth helper always resolve to the same file.

### Apptainer bind (`runtimes/_apptainer_auth.py::auth_argv`)

For the default (host-live) path, the runtime emits:

```
--bind <cred_file>:/tmp/sac-claude/.credentials.json:rw
--env  CLAUDE_CONFIG_DIR=/tmp/sac-claude
```

The **`:rw`** is the reason live refresh works: when the bundled
`claude` inside the container detects the access token nearing expiry
(~5 min skew) it writes the new token back through the bind to the
host canonical. Without `:rw` the file would be frozen and the
container would 401 the moment the token expired.

The target lives under `/tmp/` (not `$HOME`) because the D2 hardened
preflight requires `$HOME` to be empty; pointing `CLAUDE_CONFIG_DIR`
at `/tmp/sac-claude` keeps both the SDK and the CLI in sync without
polluting `$HOME`.

### Per-account COPY caveat (`runtimes/_apptainer_creds.py::resolve_cred_file`) — **write-back is lost**

When `spec.claude.account` is non-empty, `resolve_cred_file`
**`shutil.copy2`**s the store snapshot into the agent's own state dir
and returns that path; the caller binds **the copy** `:rw`. So:

- The container's refresh writes into the **agent-local copy**, not the
  host canonical.
- The host canonical never advances.
- The agent stays alive until the refresh token in its frozen copy
  itself dies, then 401s with no host-visible recovery.

**Implication for a refresher agent:** do **not** use
`spec.claude.account`. Bind the host canonical directly via an
explicit `spec.apptainer.binds` entry and a custom `CLAUDE_CONFIG_DIR`,
so the refresh **writes through** to the canonical the rest of the
fleet shares:

```yaml
apptainer:
  binds:
    - "/home/<user>/.claude/.credentials-<account>.json:/tmp/sac-claude-<account>/.credentials.json:rw"
env:
  CLAUDE_CONFIG_DIR: "/tmp/sac-claude-<account>"
# leave spec.claude.account UNSET
```

### Preflight (`_state/_preflight_creds.check_oauth_token_expiry`) — what it does NOT do

`EXPIRY_SKEW_SECONDS = 300`. The preflight reads
`data["claudeAiOauth"]["expiresAt"]` (ms or s; auto-detected via
`> 1e12`) and refuses to start if the **access** token is within 300 s
of expiring or already dead. It is also **skipped entirely** when any
API-key env is set (`_api_key_env_is_set`).

It does **not** inspect the refresh token. It does **not** make a
network call. So:

> A direct-bind agent **starts** (preflight passes if access still has
> more than five minutes of life) then fails **LOUD** at the **first
> turn** with 401 if the bound file is in fact dead (e.g. refresh
> token expired, account revoked).

This is the **start-then-fail-loud** mode — the agent's first 401 is
the first authoritative signal that the canonical needs operator
recovery.

## 3. Auto-refresh — the bundled CLI does it; sac just keeps a session live

The bundled `claude` renews the **access** token in place ~5 min before
expiry **while a session is active**. With the `:rw` bind the new
token persists to the host canonical; every other agent and refresher
sharing that canonical sees the fresh token on its next read.

So the canonical stays fresh **as long as something is talking to the
account**. The standard pattern:

- **One per-account refresher agent** on the cheapest model
  (`claude-haiku-4-5`), direct-bound to the canonical
  (`/home/<user>/.claude/.credentials-<account>.json`) with a custom
  `CLAUDE_CONFIG_DIR` — see the YAML snippet above.
- **A 30-minute keepalive cron** that posts an A2A turn to each
  refresher: `sac agents send <refresher> hello`. The turn forces the
  bundled CLI to spin a session, observe the impending expiry, and
  rotate the access token through the bind back to the canonical.

Every other agent on the same account benefits transparently — they
all bind the same canonical.

## 4. Expired ≠ unrecoverable — until the refresh token dies

The credentials JSON carries both an `accessToken` (~8 h life) and a
`refreshToken` (multi-hour to multi-day). The bundled CLI quietly swaps
an expired access token for a new one **using the refresh token**.

What dies in stages:

- **Access expired, refresh alive** → CLI rotates silently; no operator
  action.
- **Access expired, refresh expired** → CLI cannot self-revive; the
  next session 401s. **Re-login is required.**

Verified: the `scitex-ai` canonical hit an expired refresh; the
refresher 401'd with no self-revival path. Re-login (§5) cleared it.

## 5. Verified re-login flow — headless, operator-in-loop

The bundled CLI's auth subcommands are:

```
claude auth login        # OAuth login (code-paste flow)
claude auth logout
claude auth status
```

`login` is **not** a localhost redirect — `redirect_uri` is
`https://platform.claude.com/oauth/code/callback`, and the resulting
page shows a **code** of the form `<code>#<state>` for the operator to
paste back. So:

- The URL alone is not enough — the operator needs a way to **return
  the code into the CLI's prompt**.
- A direct-injection path (`tmux send-keys`, or an A2A turn that pipes
  to the running `claude auth login` process) is required.

### Step-by-step (verified ywata-note-win 2026-05-26)

1. **Back up the live canonical** (always — so the lead/host session is
   never disturbed by a botched login):

   ```
   /bin/cp -f ~/.claude/.credentials.json ~/.claude/.credentials.json.bak
   ```

2. **Run login in an isolated `CLAUDE_CONFIG_DIR` via tmux**, so the
   host `~/.claude/` is untouched:

   ```
   tmux new -d -s login-<acct> "
     CLAUDE_CONFIG_DIR=/tmp/login-<acct> claude auth login
   "
   ```

3. **Capture the pane and reassemble the OAuth URL.** The URL **wraps
   across pane lines** (no inserted spaces). Programmatically join the
   wrapped lines into a single URL — do **not** rely on a single-line
   grep.

4. **Surface only the URL** to the operator, with **which account** is
   being logged in (so the operator picks the right Anthropic account
   in the browser). Do **not** print token values; do not print the
   refresh/access fields. Only `expiresAt` and `subscriptionType` are
   safe to log later.

5. **Operator authorizes** the correct account; `platform.claude.com`
   shows a code as `<code>#<state>` (single-use, never log it).

6. **Inject the full `<code>#<state>` into the prompt** (it says
   `Paste code here >`):

   ```
   tmux send-keys -t login-<acct> "<code>#<state>" Enter
   ```

   The CLI prints `Login successful` and writes
   `/tmp/login-<acct>/.credentials.json` (`expiresAt` ~+8 h,
   `subscriptionType=max`).

7. **Promote to the canonical** (note `/bin/cp` to bypass any aliased
   `cp` — see §7):

   ```
   /bin/cp -f /tmp/login-<acct>/.credentials.json \
             ~/.claude/.credentials-<acct>.json
   chmod 600 ~/.claude/.credentials-<acct>.json
   ```

   If `<acct>` is the active account, also re-point the symlink:

   ```
   ln -sfn ~/.claude/.credentials-<acct>.json ~/.claude/.credentials.json
   ```

8. **Restart the per-account refresher** (the `--yes` is mandatory —
   see §7):

   ```
   sac agents restart cred-refresher-<acct> --yes
   ```

9. **Verify**:

   ```
   sac agents send cred-refresher-<acct> hello
   # expect a plain `ok`-style reply, NOT a 401
   ```

## 6. 401-recovery layer (design)

Hook the refresher (or the keepalive cron) so that on a 401 it:

1. Spawns `claude auth login` in an isolated `CLAUDE_CONFIG_DIR`.
2. Captures + reassembles the wrapped URL.
3. Notifies the operator with `{machine, host, agent, account, URL}` —
   typically via the Telegram MCP tools (see
   [23_telegram-integration.md](23_telegram-integration.md)) so the
   operator can one-click + reply with the code.
4. Pastes the returned `<code>#<state>` via `tmux send-keys` (or an
   A2A turn carrying the code) into the running login process.
5. Promotes the new credentials JSON to the canonical (§5 step 7)
   and restarts the refresher (§5 step 8).
6. The whole fleet on that account recovers automatically — they all
   bind the same canonical.

Because the flow is **code-paste**, URL-click-only is insufficient.
Plan for a direct-injection return path (tmux send-keys or an A2A
turn) from day one.

## 7. Gotchas

- **`/bin/cp -f` (not `cp -f`).** A shell `cp` alias/function on the
  host commonly maps to `cp -i`, which then prompts to overwrite and,
  in a non-interactive context, silently no-ops. Always promote via
  the bare binary.
- **`sac agents restart` needs `--yes`.** Without it the CLI prompts
  for confirmation and a non-interactive call hangs.
- **The URL wraps across tmux pane lines.** Reassemble by joining
  lines (no inserted spaces); a naive `grep '^https://'` only sees
  the first fragment.
- **The pasted value is `<code>#<state>`** — the literal `#` and
  `<state>` are part of the payload, not a comment or shell pipe.
- **Never print token values.** Log only `expiresAt` and
  `subscriptionType`. The OAuth code is single-use but still sensitive
  (it grants account access for the duration of the exchange).
- **Always log in inside an isolated `CLAUDE_CONFIG_DIR`** and back up
  the live canonical first. The lead/host session must never be
  disturbed by a refresh in progress.

## See also

- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is the wrong place for credentials and how
  `setting_sources=[]` isolates the agent from host `~/.claude`
  auto-discovery.
- [40_troubleshooting.md](40_troubleshooting.md) — generic 401 / start
  failure debugging.
- Source files cited: `runtimes/_sdk_common.py`
  (`provision_anthropic_auth`, `_cred_file_path`),
  `runtimes/_apptainer_auth.py` (`auth_argv` — the `:rw` bind at
  `/tmp/sac-claude/.credentials.json` + `CLAUDE_CONFIG_DIR`),
  `runtimes/_apptainer_creds.py` (`resolve_cred_file` — the per-account
  COPY behavior), `_state/_preflight_creds.py`
  (`check_oauth_token_expiry`, `EXPIRY_SKEW_SECONDS=300`).
