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

> **THE HOST HAS ALREADY PICKED: this hand-maintained unit.** It was
> installed 2026-07-05 14:38 (`Restart=always`) and has supervised the
> daemon ever since — `NRestarts=0`. **Do NOT run
> `scitex-dev service ensure sac.listen`.** It does not adopt this unit:
> scitex-dev derives the unit name from the job name VERBATIM, so it
> installs a SECOND unit, `sac.listen.service` (a DOT), beside the
> `sac-listen.service` (a HYPHEN) already running. systemd treats them as
> unrelated, and you get exactly the double-unit fight this file warns
> about below — with every lost round destroying the in-memory Broker and
> deafening every agent's inbox.
>
> The `sac.listen` JobSpec was therefore REMOVED from `provide_jobs()`
> (a test now pins its absence). PR #543 added it on the premise that
> listen "had NO SUPERVISOR" — false: this unit was created the same day
> that PR was opened, and the PR then sat 9 days and merged unre-checked.
> Read the rest of this section before re-federating anything.
>
> That verb resolves the name from the `scitex_dev.jobs` federation and
> then — in ONE idempotent step — writes the `.service` unit,
> `daemon-reload`s, and `enable --now`s it (falling back to a respawn
> keep-alive loop under `~/.scitex/<pkg>/runtime/` on hosts with no
> reachable systemd `--user` manager). It is the purpose-built consumer
> for `kind="service"`; `scitex-dev ecosystem systemd install` is the
> *timer* installer and does not start a service.
>
> This is an ALTERNATIVE activation path to the hand-maintained unit
> below — the two are NOT meant to run simultaneously (both would try
> to bind `127.0.0.1:7878`; the flock-backed single-instance guard
> stops a double *process*, but a double *unit* still fights over
> restarts). Pick ONE:
>
> * **`scitex-dev service ensure sac.listen`** — federated unit, one
>   idempotent command, single source of truth with the rest of the
>   ecosystem's jobs, and the only path that also works on a host with
>   no systemd `--user` manager. Covers **every** non-`systemctl stop`
>   exit via `Restart=always` — including the CLEAN 0-exit shutdown
>   that is how this daemon has actually been lost in production
>   (2026-07-05, and again 2026-07-14), with nothing bringing it back.
>   Does **not** detect a wedged-but-alive process.
> * `scripts/systemd/install-sac-listen.sh` — hand-maintained unit plus
>   the `sac-listen-health.timer` wedge-detection watchdog, i.e. hang
>   coverage on top of crash coverage. Choose this if wedge detection
>   is worth hand-maintaining the unit; note the probe restarts
>   `sac-listen.service` **by name**, so it does not compose as-is with
>   the federated `sac.listen.service` unit.
>
> Wedge detection has no equivalent in `scitex_dev.jobs` yet
> (`JobSpec.watchdog_sec` drives systemd's `sd_notify` watchdog, which
> `sac listen` does not implement — see the `watchdog_sec` note in
> `_jobs_plugin.py`). Closing that gap in the federated path — a
> health-probe primitive keyed on an HTTP endpoint — is the follow-up
> that would make the federated unit a strict superset.
>
> If you switch from the hand-maintained unit to the federated one (or
> vice versa), `systemctl --user disable --now` the one you are
> retiring first.

```bash
# Install BOTH the listen unit and its health watchdog (preferred —
# copies units + probe script, daemon-reload, enable --now, status):
scripts/systemd/install-sac-listen.sh

# Or by hand (listen unit only — does NOT install the watchdog):
cp scripts/systemd/sac-listen.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sac-listen.service

# Verify
systemctl --user status sac-listen.service
systemctl --user list-timers sac-listen-health.timer
journalctl --user -u sac-listen.service -n 50
journalctl --user -u sac-listen-health.service -n 50   # LOUD restart/alarm lines

# Healthcheck (echos the same /v1/health the unit's diagnostic
# stderr names at boot — and the same URL the watchdog probes)
curl -s http://127.0.0.1:7878/v1/health   # → {"ok":true,"service":"sac-listen","v":1}

# Disable / remove (preferred — removes units + watchdog + probe)
scripts/systemd/install-sac-listen.sh --uninstall
```

### Restart policy + companion guards

* `Type=simple` + `Restart=always` + `RestartSec=5s`. Brief debounce
  so a launch that always fails (e.g. bad pip upgrade surfaced as
  ImportError) doesn't hot-loop. **Incident 2026-06-26** upgraded this
  from `Restart=on-failure` to `Restart=always`: a clean 0-exit (e.g.
  an unexpected SIGTERM that uvicorn turns into a graceful shutdown)
  was NOT covered by `on-failure`, and that is exactly how the fleet
  lost a2a comms silently with nothing restarting the listen.
  `StartLimitIntervalSec=60s` / `StartLimitBurst=5` (in `[Unit]`)
  rate-limit a permanently-broken launch so it can't spin forever; the
  health watchdog clears the resulting `failed` state and keeps trying.
* **Decoupled from agent/lead lifecycle.** The unit declares NO
  `After=`/`Requires=`/`BindsTo=`/`PartOf=` against any agent or lead
  unit — only `After=network.target`. Retiring, restarting, or
  crashing any agent (or the lead) must NEVER take the listen down;
  it is fleet infrastructure.

### Health watchdog — sac-listen-health.{service,timer}

`Restart=always` only sees a *process exit*. A **wedged** listen
(process alive, port still bound, but the HTTP server no longer
answering `/v1/health`) is invisible to systemd — and that silent
mode is what the 2026-06-26 incident punished the fleet with. The
companion watchdog closes the gap:

* `sac-listen-health.timer` fires `sac-listen-health.service` every
  ~30s (`OnBootSec=30s`, `OnUnitActiveSec=30s`).
* `sac-listen-health.service` (oneshot) runs
  `sac-listen-health-probe.sh`, which HTTP-probes
  `http://127.0.0.1:7878/v1/health`.
* The watchdog is itself decoupled — it does NOT `Requires=`/`After=`
  the listen (it must run and alarm precisely when the listen is
  down), and the timer is enabled independently.

## The health watchdog: how it decides

**Incident 2026-07-14 — the watchdog WAS the outage.** The probe used to
restart `sac-listen.service` after **ONE** failed `curl` with a **5-second**
deadline. On a box that idles at **load 60-70** that is not a health check,
it is a coin flip. Measured against a REAL server that answered `HTTP 200`
in 8s (healthy, merely *busy*):

```
probe #1 -> "sac-listen DOWN ... (transport failure)" -> RESTART
probe #2 -> "sac-listen DOWN ... (transport failure)" -> RESTART
probe #3 -> "sac-listen DOWN ... (transport failure)" -> RESTART
TOTAL RESTARTS of a HEALTHY daemon: 3 / 3
```

The daemon **answered every time**. The probe called it a "transport
failure" every time, because it could not tell SLOW from DOWN.

And the remedy is catastrophic: **every `sac listen` restart tears down the
in-memory a2a `Broker`, which deafens EVERY agent's inbox at once.** So a
slow probe did not merely mis-report — **it manufactured the outage it
claimed to detect**, then re-probed *during its own restart*, saw a
genuinely-down daemon, and restarted again. Live journal, 26s apart:

```
10:57:26  health-probe: ERROR ... incident-class=sac-listen-watchdog
10:57:26  systemd: Stopping sac listen            <- restart #1 (of a HEALTHY daemon)
10:57:45  Started sac listen
10:57:52  health-probe: ERROR: sac-listen DOWN    <- probed DURING its own restart
10:57:52  watchdog is RESTARTING                  <- restart #2, 26s later
```

**A probe that mutates is not a probe.** That single 5-second fuse plausibly
explains the listen churn (3 pids in 7 min), the "registered but deaf"
agents, the duplicate standby loops, and the auth pool appearing to
"re-wedge within a minute of a drain" (it was not a wedge — it was a restart
severing in-flight requests).

Both directions are bugs: never acting hides an outage, over-acting
**creates** one. The false-RED is the worse of the two, because its remedy
destroys a healthy thing. So **only a corroborated verdict may restart.**

### 1. Three states, never a bool

Two states cannot express *"I asked and got nothing"* — which is precisely
what a loaded box produces.

| Verdict | Meaning | Weight |
|---|---|---|
| **UP** | It **answered** — any HTTP status `< 500` (see §2) | — |
| **DOWN** | Connection **REFUSED** — the kernel sent RST: *nothing is listening* | **2** (hard) |
| **DOWN** | **HTTP 5xx** — it answered, but its health route is erroring. Bound and speaking HTTP, yet not healthy | 1 (soft) |
| **UNKNOWN** | We asked and got **nothing** (timeout / DNS / reset). Under load this is what a HEALTHY-but-busy daemon looks like | 1 |

**Absence of evidence is not evidence of death.** A timeout is UNKNOWN, and
`curl`'s `%{time_connect}` is used to say *why*: if the TCP handshake
completed, the port **is** bound and the daemon has not exited — so the
evidence line reads "no HTTP answer within 20s (but TCP connected — the port
IS bound)". That is the difference between *busy* and *gone*.

### 2. Any HTTP status < 500 is "UP" (deliberate — keep it)

A `401`/`403` **proves** the daemon is up: bound, speaking HTTP, and
auth-gating. Card `sac-listen-restart-healthcheck-bearer` (PR #463) exists
because gating liveness on `status == 200` re-classified a live,
401-answering daemon as "down" — a false-RED that killed a **healthy**
process. Only a *server error* (5xx) or *no answer at all* counts against
the daemon. Matches `_listen/_holder_health.py`.

### 3. Corroboration — and a failure is a FACT

Accumulated failure weight must reach `SAC_LISTEN_FAIL_THRESHOLD` (3):

* **2 consecutive REFUSALS** (2+2) → act fast. **Crash coverage** — a
  genuinely dead listen still comes back (incident 2026-06-26).
* **3 consecutive UNKNOWNs** (1+1+1) → act. **Wedge coverage** — a daemon
  that cannot answer a trivial route within a 20s deadline, three times
  running, is not "busy". Corroboration is what **promotes** a repeated
  UNKNOWN into a DOWN verdict.

**A single success does not wipe the ledger.** That bug — `consecutive = 0`
reset by any one lucky reply — is this class's *other half*, and a peer just
fixed it in `sac listen`'s own holder check (PR #673,
`_listen/_standby_ledger.py`), where a flapping holder oscillated
`1/2` → "healthy" → `1/2` forever and was **never acted on**. Here a success
builds a **serving streak**, and the ledger clears only after
`SAC_LISTEN_RECOVERY_STREAK` (2) consecutive UPs — logged **LOUD**, because
"the thing I said was broken now looks fine" is exactly the transition an
operator must never have hidden from them.

Blip once and you are not destroyed; keep failing and you are always
eventually healed.

### 4. Never restart something that is already restarting

Two **independent** guards, so losing one cannot resurrect the 26s
double-restart:

* **(a) Post-restart backoff** — after issuing a restart the probe does not
  probe **at all** for `SAC_LISTEN_RESTART_BACKOFF` (90s). Restart #2 above
  happened *because* it probed during its own restart. You cannot draw a
  conclusion about a daemon you are in the middle of restarting.
* **(b) Unit-state guard** — if systemd reports the unit `activating`, it is
  already coming back (someone else restarted it, or it crashed and
  `Restart=always` caught it) → stand down. This holds **even if the state
  file is lost or corrupt.**

### 5. Rate-limit the remedy

At most `SAC_LISTEN_MAX_RESTARTS` (2) per `SAC_LISTEN_RESTART_WINDOW`
(600s). Beyond that the watchdog **ALARMS LOUDLY and STOPS restarting**
(`incident-class=sac-listen-watchdog-giving-up`, exit 1). If 2 restarts did
not fix it, the 3rd will not either — and an unbounded restarter on a bad
signal is how you take a fleet down at 3am. A human is needed, and it says
so instead of thrashing.

### On a corroborated down, the script still

1. logs a **LOUD ERROR** to the journal (`journalctl --user -u
   sac-listen-health`) so the restart is VISIBLE, not silent;
2. best-effort emits the anomaly on the **one** operator alarm path
   (`sac fleet notify blocker`) — this may fail (the listen is the transport
   for that notify), but is tried so a peer/lead listen or a recovered inbox
   still surfaces it;
3. runs `systemctl --user reset-failed && restart sac-listen.service` (the
   `reset-failed` clears a tripped `StartLimitBurst` so a transient burst
   self-heals while a genuine hard-down stays visible).

### The ledger

The script is invoked **fresh** every ~30s, so an in-memory counter would
always read zero and "N consecutive failures" could never be observed. State
persists at
`~/.scitex/agent-container/runtime/listen-health.state` (override:
`SAC_LISTEN_HEALTH_STATE`) as plain `key=value`. It is **never `source`d** —
a corrupt or hostile state file must not become code, and any value that is
not a plain integer resets that field to `0` (**fail-safe: no history == do
not restart**).

```bash
# What is the watchdog thinking right now?
~/.config/systemd/user/sac-listen-health-probe.sh --status
# state_file:      /home/you/.scitex/agent-container/runtime/listen-health.state
# unit:            sac-listen.service (ActiveState=active)
# probe_timeout:   20s (connect 5s)
# failure_weight:  0 / 3  (weight required to restart)
# serving_streak:  0 / 2  (consecutive UPs that clear the ledger)
# restarts_in_600s: 0 / 2
# last_restart:    never
```

### Tunables (env on the probe)

| Var | Default | Meaning |
|---|---|---|
| `SAC_LISTEN_HEALTH_URL` | `http://127.0.0.1:7878/v1/health` | what to probe |
| `SAC_LISTEN_UNIT` | `sac-listen.service` | what to restart |
| `SAC_LISTEN_PROBE_TIMEOUT` | `20` | total curl deadline, s. **Was 5** — the fuse that caused the incident. The live daemon answers in **~30 ms**, so 20s is ~600× the median and still catches a wedge. |
| `SAC_LISTEN_CONNECT_TIMEOUT` | `5` | TCP-connect deadline, s |
| `SAC_LISTEN_FAIL_THRESHOLD` | `3` | failure weight required to act |
| `SAC_LISTEN_RECOVERY_STREAK` | `2` | consecutive UPs that clear the ledger |
| `SAC_LISTEN_RESTART_BACKOFF` | `90` | no-probe window after a restart, s |
| `SAC_LISTEN_MAX_RESTARTS` | `2` | restarts allowed per window |
| `SAC_LISTEN_RESTART_WINDOW` | `600` | rate-limit window, s |
| `SAC_LISTEN_NOTIFY` | `1` | `0` skips `sac fleet notify` (tests / non-lead hosts) |
| `SAC_LISTEN_HEALTH_STATE` | `~/.scitex/agent-container/runtime/listen-health.state` | ledger path |

Modes: `--check-only` (pure observation — **zero** side effects, writes no
state), `--status` (dump the ledger), `--reset` (clear it).

Behaviour is pinned by
`tests/integration/test_sac_listen_health_watchdog_decision.py`, which drives
the **real** script against **real** HTTP servers on real ephemeral sockets
(slow / refusing / 5xx-ing / 401-ing / dying) and asserts the decision each
time. No mocks; nothing there ever touches port 7878.
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
* **Restarting sac-listen does NOT redeploy in-SIF code changes.**
  Agents keep running their baked-in `:scitex` SIF copy of sac. After
  merging anything under `_runners/`, `_lifecycle/_in_sif_*`,
  `_mcp/*`, or anywhere else the in-SIF runner imports, you must
  rebuild the SIF and restart the agents. See
  [`docs/deploy-runbook.md`](../../docs/deploy-runbook.md) for the
  full "merged ≠ deployed" checklist.

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

# sac.host-sync-check — federated timer (peer drift alarm)

A SECOND federated `scitex_dev.jobs` job (same mechanism as above;
`_jobs_plugin.py`). Runs `sac host sync --check --all --alarm` hourly:
the **read-only** one-way-sync drift detector (mutates no peer), with
`--alarm` routing each verdict to an idempotent scitex-todo card
(`host-sync-drift-<peer>`, `status=blocked`/`blocker=operator-decision`)
— upsert on drift/unknown, resolve on clean. This makes the Stage-0
detector (PR #690), previously scheduled nowhere, actually RUN and be
SEEN on the board instead of in a log nobody reads. Install with
`sac dev systemd install --yes`, then
`systemctl --user enable --now sac.host-sync-check.timer`.
