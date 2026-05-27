---
description: |
  [TOPIC] scitex-agent-container — verified re-login flow + 401-recovery design for SAC OAuth credentials.
  [DETAILS] Headless tmux-based code-paste flow; the auth subcommand surface; isolated `CLAUDE_CONFIG_DIR` so the host canonical is never disturbed; `/bin/cp -f` and `--yes` gotchas; 401-recovery hook design via Telegram for operator-in-loop. Companion to 26_credentials-rotation.md, which covers the on-disk model + auto-refresh mechanics.
tags: [scitex-agent-container-credentials-relogin]
---

# SAC OAuth credentials: re-login + 401 recovery

> Companion to [26_credentials-rotation.md](26_credentials-rotation.md).
> 26_ covers the canonical-symlink model, the `:rw` bind, and the auto-refresh
> path. This skill covers what to do when **the refresh token has actually
> died** and the bundled CLI can't self-revive.

## Verified re-login flow — headless, operator-in-loop

Verified on `ywata-note-win` 2026-05-26.

The bundled CLI's auth surface:

```
claude auth login        # OAuth login (code-paste flow)
claude auth logout
claude auth status
```

`login` is **not** a localhost redirect — `redirect_uri` is
`https://platform.claude.com/oauth/code/callback`, which shows a **code**
of the form `<code>#<state>` for the operator to paste back. The URL alone
is not enough; the operator needs a way to **return the code into the
CLI's prompt**. A direct-injection path (`tmux send-keys`, or an A2A turn
piped to the running `claude auth login` process) is required.

### Step-by-step

1. **Back up the live canonical** so the lead/host session is never
   disturbed by a botched login:

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
   across pane lines** (no inserted spaces). Join the wrapped lines
   programmatically — do **not** rely on a single-line grep.

4. **Surface only the URL** to the operator with **which account** is
   being logged in (so the operator picks the right Anthropic account in
   the browser). Do **not** print token values.

5. **Operator authorizes** the correct account; `platform.claude.com`
   shows a code as `<code>#<state>` (single-use, never log it).

6. **Inject the full `<code>#<state>` into the prompt**:

   ```
   tmux send-keys -t login-<acct> "<code>#<state>" Enter
   ```

   The CLI prints `Login successful` and writes
   `/tmp/login-<acct>/.credentials.json` (`expiresAt` ~+8 h).

7. **Promote to the canonical** (`/bin/cp` to bypass any aliased `cp`):

   ```
   /bin/cp -f /tmp/login-<acct>/.credentials.json \
             ~/.claude/.credentials-<acct>.json
   chmod 600 ~/.claude/.credentials-<acct>.json
   ```

   If `<acct>` is the active account, re-point the symlink:

   ```
   ln -sfn ~/.claude/.credentials-<acct>.json ~/.claude/.credentials.json
   ```

8. **Restart the per-account refresher** (the `--yes` is mandatory):

   ```
   sac agents restart cred-refresher-<acct> --yes
   ```

9. **Verify**:

   ```
   sac agents send cred-refresher-<acct> hello
   # expect a plain `ok`-style reply, NOT a 401
   ```

## 401-recovery layer (design)

Hook the refresher (or the keepalive cron) so that on a 401 it:

1. Spawns `claude auth login` in an isolated `CLAUDE_CONFIG_DIR`.
2. Captures + reassembles the wrapped URL.
3. Notifies the operator with `{machine, host, agent, account, URL}` —
   typically via Telegram MCP tools (see
   [23_telegram-integration.md](23_telegram-integration.md)) for one-click
   + reply with the code.
4. Pastes the returned `<code>#<state>` via `tmux send-keys` (or an A2A
   turn carrying the code) into the running login process.
5. Promotes the new credentials JSON to the canonical (step 7 above) and
   restarts the refresher (step 8 above).
6. The whole fleet on that account recovers automatically — every agent
   binds the same canonical.

Because the flow is **code-paste**, URL-click-only is insufficient. Plan
for a direct-injection return path from day one.

## Gotchas

- **`/bin/cp -f` (not `cp -f`).** A host `cp` alias/function commonly maps
  to `cp -i`, which prompts in non-interactive contexts and silently
  no-ops. Always promote via the bare binary.
- **`sac agents restart` needs `--yes`.** Without it the CLI prompts and a
  non-interactive call hangs.
- **The URL wraps across tmux pane lines.** Reassemble by joining lines
  (no inserted spaces); a naive `grep '^https://'` sees only the first
  fragment.
- **The pasted value is `<code>#<state>`** — the literal `#` and
  `<state>` are part of the payload, not a comment or shell pipe.
- **Never print token values.** Log only `expiresAt` and
  `subscriptionType`. The OAuth code is single-use but still sensitive.
- **Always log in inside an isolated `CLAUDE_CONFIG_DIR`** and back up the
  live canonical first.

## See also

- [26_credentials-rotation.md](26_credentials-rotation.md) — on-disk
  model, auto-refresh mechanics, the per-account COPY caveat.
- [25_claude-setup-delivery.md](25_claude-setup-delivery.md) — why
  `to_home/` is the wrong place for credentials.
- [40_troubleshooting.md](40_troubleshooting.md) — generic 401 debugging.
