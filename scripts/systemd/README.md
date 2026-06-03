# sac systemd-user units

This directory ships the systemd-user units sac installs into
`~/.config/systemd/user/`. There are two kinds:

* **Federated scheduled jobs** — registered into the
  `scitex_dev.jobs` ecosystem entry-point group and materialised by
  `sac dev systemd install` from a single `JobSpec` source of truth.
  Today: `sac.accounts-refresh.{service,timer}`. The unit files are
  NOT hand-maintained here.

* **Hand-maintained long-running services** — `Type=simple` daemons
  that do not fit the `JobSpec` (no cron schedule). Today:
  `sac-listen.service` (the host-level HTTP/JSON control plane).
  Operator-mandated 2026-06-01 (task #26): auto-start on boot,
  auto-restart on crash, journal-tail-able.

## sac-listen.service — hand-maintained

Long-running daemon (the host's `sac listen` HTTP/JSON control
plane, default loopback `127.0.0.1:7878`). Provides push hub, spawn
broker, lead inbox. Before this unit landed, the listen was
operator-started ad-hoc and could be found DOWN with nothing
restarting it.

```bash
# Install the unit (operator copies, then enables)
cp scripts/systemd/sac-listen.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sac-listen.service

# Verify
systemctl --user status sac-listen.service
journalctl --user -u sac-listen.service -n 50

# Healthcheck (echos the same /v1/health the unit's diagnostic
# stderr names at boot)
curl -s http://127.0.0.1:7878/v1/health   # → {"ok":true,"service":"sac-listen","v":1}

# Disable / remove
systemctl --user disable --now sac-listen.service
rm ~/.config/systemd/user/sac-listen.service
systemctl --user daemon-reload
```

### Restart policy + companion guards

* `Type=simple` + `Restart=on-failure` + `RestartSec=5s`. Brief
  debounce so a launch that always fails (e.g. bad pip upgrade
  surfaced as ImportError) doesn't hot-loop.
* The companion `_listen/_single_instance.py` flock guard (task
  #26 sub (1)) ensures `Restart=on-failure` can't double-bind the
  port. The kernel releases the flock on every dirty exit, so the
  next start cleanly takes it.
* Agents already auto-reconnect their SSE inbox subscriptions on
  listen restart via the exponential-backoff loop in
  `_mcp/channel.py` (verified by
  `tests/scitex_agent_container/_mcp/test_channel_reconnect.py`,
  task #26 sub (2)). A `systemctl --user restart sac-listen` does
  NOT require any agent restart.

### Operational gotcha: SIGTERM hang holding the lockfile

Observed during the 2026-06-03 host listen restart (lead): a SIGTERM
that releases the port but **doesn't fully exit** (sub-second hang
during in-flight SSE shutdown) leaves the flock-backed
`listen-<port>.pid` file behind. The next start refuses with:

```
another sac listen already running (pid <N>)
```

even though `<N>` is dead. The kernel releases the flock on dirty
exit, but the pidfile on disk is independent of the flock and only
gets cleared by a CLEAN shutdown.

#### Canonical recovery — `sac listen restart`

```bash
sac listen restart                    # default 10s TERM grace, then SIGKILL
sac listen restart --grace-secs 30    # longer TERM window
sac listen restart --force            # skip TERM, go straight to SIGKILL
```

The verb codifies the entire SIGTERM → wait → SIGKILL fallback →
`rm -f` pidfile → relaunch → health-check sequence atomically:

1. Reads the PID from `~/.scitex/agent-container/runtime/listen-<port>.pid`.
2. Sends SIGTERM, polls every 200ms up to `--grace-secs` (default 10s).
3. Escalates to SIGKILL if the daemon survives the deadline. Prints a
   LOUD WARN to stderr on escalation so the diagnostic is visible:
   ```
   WARN: escalated to SIGKILL after 10.0s; daemon hung on SIGTERM
   (likely in-flight SSE shutdown). See scripts/systemd/README.md for
   the manual recovery.
   ```
   (Silent on a clean TERM exit.)
4. Verifies the PID is actually dead before clearing the pidfile
   (defence-in-depth against killing a recycled PID).
5. Relaunches via `systemctl --user daemon-reload && systemctl --user
   restart sac-listen.service` if the unit is installed and enabled;
   otherwise direct `sac listen` spawn.
6. Polls `/v1/sac/health` until 200 or 30s deadline. Exits non-zero
   with the actionable error if the new daemon doesn't come up.

The sequence is **non-destructive** — state.db is persistent on disk,
agents auto-reconnect their SSE streams on the new listen via the
existing backoff loop, and the flock guard means a second concurrent
listen process cannot bind the port even if the recovery race ran
twice.

#### Manual fallback — when `sac` itself is broken

If sac is uninstalled / the venv is broken / `sac --version` doesn't
resolve, the verb above can't run. The manual sequence still works
verbatim and is what the verb does internally:

```bash
# 1. Identify the named pid in the stale lockfile (path varies by --bind)
pid=$(cat ~/.scitex/agent-container/runtime/listen-7878.pid 2>/dev/null) && echo "named: $pid"

# 2. Check if it's actually alive
kill -0 "$pid" 2>/dev/null && echo ALIVE || echo DEAD-STALE-LOCK

# 3. If DEAD: harmlessly re-send SIGKILL (no-op if already gone),
#    clear the stale pidfile, then restart through the unit
kill -9 "$pid" 2>/dev/null
rm -f ~/.scitex/agent-container/runtime/listen-7878.pid
systemctl --user daemon-reload
systemctl --user restart sac-listen.service

# 4. Verify
systemctl --user status sac-listen.service
curl -s http://127.0.0.1:7878/v1/sac/health
```

### Notes

* `ExecStart=/usr/bin/env sac listen` resolves `sac` against the
  user's `$PATH`. If the operator runs sac out of a venv, ensure
  the venv's `bin/` is on the user's default PATH (systemd-user
  inherits `~/.profile` style env via PAM, NOT interactive
  `~/.bashrc`). Add `Environment=PATH=...` to a drop-in if needed.
* `StandardOutput=journal` / `StandardError=journal` route both
  the listen-boot diagnostic lines (token file / pidfile / health
  URL) and uvicorn's request log to `journalctl --user -u
  sac-listen`.
* No `--bind` override: the default `127.0.0.1:7878` is correct
  for the supported deployment shape (orochi owns the tunnel
  mesh; SAC_OROCHI_SCOPES.md §4.4). Override via a `systemctl
  --user edit sac-listen` drop-in if a non-loopback bind is ever
  needed.

---

# sac accounts refresh — federated systemd-user timer

Headless rotation of the Claude Code OAuth access-token using the
long-lived refresh-token stored under
`~/.scitex/agent-container/accounts/<name>/.credentials.json`. Removes
the need for routine manual `claude /login` for stored accounts.

## The unit files are no longer hand-maintained here

This job is now **federated into `scitex_dev.jobs`** (the ecosystem-wide
scheduled-job registry). sac registers a single `JobSpec`
(`sac.accounts-refresh`) via the `scitex_dev.jobs` entry-point
(`src/scitex_agent_container/_jobs_plugin.py`), and scitex-dev generates
the `.service` + `.timer` unit files from that single source of truth.

The previously committed static `sac-accounts-refresh.service` /
`.timer` templates were **removed** to avoid a second, drifting copy of
the policy (they were pinned to the old `--all`, every-4h cadence).

## Policy (current)

| Field                | Value                                      |
| -------------------- | ------------------------------------------ |
| Command              | `sac accounts refresh --all --skip-active` |
| Cadence              | every **2h** (`OnUnitActiveSec=2h`)        |
| After boot/login     | `OnBootSec=15min`                          |
| Timeout              | `TimeoutStartSec=120s`                     |

`--skip-active` excludes the account matching the currently-active
`~/.claude` login so the in-use refresh_token is never rotated out from
under the live session.

## Install / uninstall

Generate and install the unit files via sac's federated wrapper (which
delegates to scitex-dev's ecosystem aggregator):

```bash
# Inspect what would be installed
sac dev systemd list

# Install (writes ~/.config/systemd/user/sac.accounts-refresh.{service,timer})
sac dev systemd install --yes
systemctl --user daemon-reload
systemctl --user enable --now sac.accounts-refresh.timer

# Verify
systemctl --user list-timers sac.accounts-refresh.timer
journalctl --user -u sac.accounts-refresh.service -n 50

# Remove
sac dev systemd uninstall --yes
```

Equivalent direct scitex-dev invocation:

```bash
scitex-dev ecosystem systemd install --name sac.accounts-refresh --yes
```

> Requires `scitex-dev>=0.16.0` (the release that adds `scitex_dev.jobs`).
> Older scitex-dev installs make `sac dev systemd` print an upgrade hint
> instead of failing.

The service is read-only with respect to source code; the only state
mutation is atomic write-back of the refreshed access_token to the
per-account credentials file. The unit exits non-zero only when EVERY
targeted account's refresh failed — that's the operator's signal that a
real `claude /login` is finally needed.
