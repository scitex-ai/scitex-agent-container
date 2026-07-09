---
description: |
  [TOPIC] scitex-agent-container — host-side credential refresh ops + the one-refresher-per-account invariant.
  [DETAILS] The bundled `claude` CLI's in-container access-token rotation + 30-min keepalive cron pattern; the one-account-one-refresher invariant that makes server-side refresh-token rotation safe; the `sac.accounts-refresh` host systemd-user timer with `--include-active --sync-active-login` (the sole refresher under the master-host model); the `sac accounts watch-live` daemon that mirrors host re-logins into the sac store; `scitex-dev creds rotate-all` multi-account CI rotation. The on-disk model + auth mechanics live in [26_credentials-rotation.md](26_credentials-rotation.md).
tags: [scitex-agent-container-credentials-rotation-host]
---

# SAC OAuth credentials: refresh + host-side ops

> Companion to [26_credentials-rotation.md](26_credentials-rotation.md)
> — that file covers what's on disk and how the SDK reaches it. This
> file covers refresh mechanics, the rotation-race invariant, and the
> host-side tooling that keeps the canonicals fresh.

## 1. Auto-refresh — the bundled CLI does it; sac keeps a session live

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
> invariant load-bearing (§2). Any out-of-band rotator (e.g., a host
> cron call against the same account) will invalidate the running
> CLI's in-memory refresh-token server-side and 401 the next turn.

## 2. One account, one refresher — the rotation-race invariant

OAuth refresh-tokens **rotate server-side on every use**: each call to
`/oauth/token` returns a new refresh-token AND invalidates the previous
one with no grace window. Combined with the in-memory cache (§1), this
means **at most one process can be the live refresher for any given
account at any given time**.

**Pre-2026-07-08 (two-refresher model, historical):** the credential
file was bound `:rw` into the container, so the in-container `claude`
CLI was itself a refresher. That meant:

* **Pinned-and-running**: the in-container CLI was the sole refresher;
  the host cron had to skip the account.
* **Host's interactive login**: the operator's interactive `claude`
  session was the sole refresher; the host cron had to skip it too
  (`--skip-active`).
* **Parked** (no pinned agent, no interactive session): the host cron
  was the sole refresher.

If two refreshers ever touched the same account concurrently, the
loser's refresh-token died the moment the winner's `/oauth/token`
returned — the loser then 401'd on its next turn even though the
snapshot file on disk looked brand-new (the winner wrote it). This was
the 2026-06-03 ~hourly 401 storm that PR #299 closed structurally, via
a `--skip-active` skip-set that was the union of the host-active
account and every account currently pinned by a running local agent.

**Since 2026-07-08 (master-host single-refresher model, current):**
the credential file is bound `:ro` into every container (both
pinned-and-running and interactively-logged-in cases go through the
same `<container_home>/.claude/.credentials.json:ro` bind — see
`runtimes/_apptainer_auth.py::credentials_file_bind`). No in-container
`claude` can refresh anything anymore, pinned or not. **The host cron
is now the SOLE refresher for every account, full stop** — so it MUST
run `--include-active --sync-active-login`, not `--skip-active`.
Running `--skip-active` under this model doesn't guard a race (there
is no other refresher left to race); it just starves the active
account's access_token until it expires and 401s the whole fleet
(the 2026-07-09/10 total-fleet stall — `_jobs_plugin.py`'s JobSpec
still said `--skip-active` for a full day after the bind flipped to
`:ro`, exactly this kind of SSOT drift).

The historical `--skip-active` skip-set logic
(`_collect_pinned_running_accounts`, PR #299, commit `dea298d`) is
preserved as an explicit opt-in escape hatch (`sac accounts refresh
--all --skip-active`) — it is simply no longer the timer's default.
See
[ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md)
§ "Failure mode 2" + § "Why a symlink doesn't solve this" for the
historical race, and the `credentials_file_bind` docstring for the
current `:ro` model.

## 3. Multi-account CI rotation

`for acct in ...; scitex-dev creds rotate-all --source $SAC_STORE --only pkgA --only pkgB ... --yes; done` splits CI auth across N Max accounts (lead 2026-05-29: 66 `scitex-*` divided 22-22-22 across 3). `--only` is repeatable.

Non-negotiables:

- **Silent-no-op trap.** Stale `--source` (`claudeAiOauth.expiresAt` in the past) exits **0 with zero stdout**. Verify first: `jq '.claudeAiOauth.expiresAt' <path>` vs `echo $(($(date +%s) * 1000))`.
- **`--source` MUST be the sac store**, `~/.scitex/agent-container/accounts/<acct>/.credentials.json` (kept fresh by §5's daemon; what `sac accounts list` reads). The `~/.claude/.credentials-<acct>.json` canonicals from [26_credentials-rotation.md §1](26_credentials-rotation.md) are refreshed via `:rw` agent binds, NOT direct rotation — using them as `--source` silently fails when stale.

## 4. The host refresher cron — `sac.accounts-refresh`

A federated systemd-user timer fires `sac accounts refresh --all --include-active --sync-active-login` every 2h (`OnUnitActiveSec=2h`). It iterates the account store and rotates EVERY account's tokens against `/oauth/token`, mirroring the active account's rotation back into the live `~/.claude/.credentials.json` login. Registered as the `sac.accounts-refresh` JobSpec via `_jobs_plugin.py`; install with `sac dev systemd install --yes`.

`--include-active` skips nothing — under the current `:ro`-everywhere model (§2) there is no other refresher left to race, so every stored account (including the host-active login and any pinned-running account) is a valid rotation target. Diagnostic stderr announces the intent:

```
[include-active] refreshing ALL accounts including the active + pinned-running ones (single-refresher model).
```

The historical `--skip-active` skip-set (union of the host-active login via `_resolve_active_account_name` and every account pinned by a running local agent via `_collect_pinned_running_accounts`, post-PR #299, commit `dea298d`) still exists and is still tested, but is now an explicit opt-in rather than the timer's default:

```
[skip-active] excluding active account 'ywatanabe-gmail-com'.
[skip-active] excluding pinned-running account 'wyusuuke-gmail-com' (refresh-token rotation race guard).
[skip-active] excluding pinned-running account 'ywatanabe-scitex-ai' (refresh-token rotation race guard).
```

Implementation: `cli_pkg/_account_refresh.py::account_refresh` (CLI wiring — `--include-active`/`--skip-active`/`--sync-active-login` are mutually-exclusive-gated), `_collect_pinned_running_accounts(home)` (reads `~/.scitex/agent-container/runtime/registry/*.json` directly to avoid the `Registry` class's import-time `REGISTRY_DIR` freeze under pytest fixtures).

## 5. The watch-live daemon — keeps the sac store fresh

`sac accounts watch-live` runs `inotifywait -m ~/.claude/` for `close_write|moved_to|create` on `.credentials.json`; atomically copies each event into the matching sac-store path (slug-map e.g. `wyusuuke@gmail.com` → `wyusuuke-gmail-com`).

Non-negotiables:

- **NOT auto-started** — no systemd / launchd unit ships. If the daemon isn't running when `claude /login` refreshes, the sac store stays stale until `sac accounts sync-live` (one-shot fallback) or the daemon starts.

Implementation: `_account/creds_watch.py` L140-152 (`watch_inotify()`), L99-133 (`watch_poll()` fallback); `_account/creds_sync.py` L141-244 (`sync_live()`), L69-77 (`slugify_email()`); `cli_pkg/_account_sync_live.py` L82-100+ (`account_watch_live` command).

## See also

- [26_credentials-rotation.md](26_credentials-rotation.md) — on-disk
  model + auth mechanics (canonical → SDK + `:rw` bind + preflight).
- [ADR-0017](../../../../docs/adr/0017-credential-rotation-and-refresh-race.md) —
  canonical write-up of the rotation model + the two failure modes
  (stale-COPY pre-#262, refresh-token race pre-#299).
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified
  re-login flow (tmux code-paste) + 401-recovery design.
