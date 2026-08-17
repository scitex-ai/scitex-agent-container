"""Federated scheduled-job provider for the ``scitex_dev.jobs`` group.

Registered via the ``scitex_dev.jobs`` entry point (see ``pyproject.toml``)
so ``scitex-dev ecosystem {cron,systemd,daemon}`` and ``sac dev`` surface
sac's own periodic jobs through the single ecosystem aggregator.

The ``scitex_dev.jobs`` import is LAZY (inside :func:`provide_jobs`) so a
scitex-dev that predates the jobs contract (PyPI lag) does not break the
entry-point's import-time metadata — the provider only needs ``JobSpec``
the moment ``discover_jobs()`` actually calls it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec


def provide_jobs() -> "list[JobSpec]":
    """Return sac's federated scheduled jobs.

    Nine jobs today:

    * ``sac.accounts-keepalive`` (``kind="timer"``) — the DISTRIBUTION half
      of the single-refresher model, and the sibling of
      ``sac.accounts-refresh`` below. That job rotates the token on the ONE
      host holding refresh material; this one COPIES the result out to the
      access-only hosts and proves each of them accepts it. Without it those
      hosts hold a credential nothing can renew and 401 within one
      access-token lifetime — measured 2026-08-10, three fleet-wide deaths
      in a day. It never mints (minting rotates, which revokes the token
      running agents are holding).

      HOST GAP, stated rather than papered over: ``JobSpec`` has no
      host-pinning field, so nothing here can declare "only on the refresh
      holder". The verb defends itself instead — ``--all`` resolves to the
      accounts THIS host holds refresh material for and exits NON-ZERO when
      that set is empty, so an install on the wrong host is loud, not quiet.

    * ``sac.freshness-refresh`` (``kind="timer"``) — the REFRESHER half of
      the version-currency check. The CLI's startup banner reads a cached
      JSON file and nothing else (no network on the hot path), so this is
      what puts an answer in that file; unscheduled, the banner has nothing
      to read and is silent forever, which reads exactly like "everything is
      fine". A check nobody schedules is not a check.

    * ``sac.fleet-reconcile`` (``kind="timer"``) — the only enforcer of
      "should be running ⇒ is running". Restarts agents whose tmux session
      is gone though their spec asks to be kept running and nothing recorded
      a deliberate stop. See its inline comment: the spec field it enforces
      (``restart.policy``) is dead code without it, and 33 agents once
      stayed dead for hours because of that.

    * ``sac.restart-login-expired-agents`` (``kind="timer"``) — the SIBLING of
      fleet-reconcile and the exact division of labor: fleet-reconcile owns
      DEAD/no-session corpses; this owns LIVE-session-but-AUTH-DEAD agents (a
      frozen "Login expired" banner) that fleet-reconcile explicitly leaves
      alone, because touching a live session destroys context. Detection is
      READ-ONLY + 2-run-corroborated; the restart runs through the pool-loading
      start path (so a timer-driven restart cannot strip an agent's CCT/Telegram
      token) and is rate-limited exactly like fleet-reconcile.

      DEPLOY GATE — declared here so the mechanism is version-controlled and
      TESTED, but it MUST NOT be enabled on a host until that host's
      ``auth-heal.py`` ``scan_tui`` cron is RETIRED. That cron already restarts
      these agents (2-run-corroborated TUI heal, since 2026-06-01); enabling
      this timer alongside it puts TWO restarters on one fleet with INDEPENDENT
      debounce state — the same double-supervisor class as the ``sac.listen``
      duplication described below, one costume over. The crontab is host state a
      PR cannot edit, so the cron retirement is an operator/dotfiles step that
      must land WITH the enable.

    * ``sac.heal-agent-auth`` (``kind="timer"``) — the INCUMBENT auth healer
      (``~/.scitex/agent-container/bin/auth-heal.py``, every 10min), declared
      here so it stops living as a hand-written crontab line that a sweep
      deletes. ``~/.dotfiles/src/.cron/copy_crontab`` installs the tracked
      manifest WHOLESALE (``git show HEAD:.crontab_list | crontab -``), so
      anything absent from ``.crontab_list`` is erased on its next run — and
      auth-heal has NO line in that manifest, which is why the wrapper that
      exports ``SAC_SECRETS_ENVRC`` kept reverting. A hand-added crontab line
      is temporary BY CONSTRUCTION; a JobSpec is not. scitex-cards and
      dotfiles moved their own schedules to systemd ``--user`` timers for the
      same reason, and scitex-dev's ruling makes the JobSpec plugin the SSoT.

      MUTUALLY EXCLUSIVE WITH ``sac.restart-login-expired-agents`` — enable
      exactly ONE. This job's ``scan_tui`` is precisely what that timer
      reimplements natively, so the deploy gate documented below is not
      lifted by declaring this one; it is made explicit. Declaring both is
      safe (a JobSpec is inert until ``ecosystem up`` installs it); ENABLING
      both puts two restarters with INDEPENDENT debounce state on one fleet.

    * ``sac.accounts-refresh`` (``kind="timer"``) — a headless OAuth
      access-token refresh for EVERY stored Claude account, including the
      active one (``--include-active``), mirroring the rotated token back
      into the live ``~/.claude`` login (``--sync-active-login``).

    * ``sac.host-sync-check`` (``kind="timer"``) — the READ-ONLY peer
      drift detector ``sac host sync --check --all``, run hourly with
      ``--alarm`` so each peer's verdict is recorded in sac's own event
      log (degraded / unknown / recovered). This is what makes the Stage-0
      detector (PR #690) actually RUN and be SEEN: shipped but scheduled
      nowhere, it was an inert alarm. The job
      mutates NOTHING on any peer — it never calls the fast-forward
      remedy (that is Stage 1). ``--alarm`` is gated to require
      ``--check`` in the CLI, so this scheduled command is read-only by
      construction.

    * ``sac.spartan-sif-bake`` (``kind="timer"``) — the EVERY-10-MINUTES
      remote SIF bake + pull. Operator directive (2026-07-17, verbatim): 「sif は最新版を
      定期焼きにしましょう。spartan 側で。それでこちらには定期的に rsync する形で。
      cpu は使わずに新しいものが得られると思います。」 The bake runs as an
      ``srun --overlap`` step inside the standing Spartan CPU lease
      (resolved BY NAME — the job id changes at every resubmit), the
      master then PULLS the gated artifact via rsync-over-ssh,
      re-verifies it locally (sha256 + apptainer-exec symbol probe) and
      only then atomically swaps the live ``sac-<layer>.sif`` symlinks.
      Keep-3 rotation on both sides. A source-unchanged run is a cheap
      SKIPPED verdict — the */10 cadence buys freshness, not transfers.

    * ``sac.worktree-gc`` (``kind="timer"``) — the DAILY worktree GC,
      ``sac worktree gc --apply --all``. Agent-tool worktrees auto-clean
      only when nothing edited them, so anything an agent TOUCHED
      persisted forever with no GC, no cap and no alarm anywhere: one repo
      reached 105 worktrees and helped trigger a host load-spike
      (``incident-worktree-sprawl-permanent-gc-20260710``). This is the
      only thing that makes the countermeasure PERIODIC — a GC nobody
      schedules is a script, not a countermeasure, which is exactly how
      the sprawl accumulated in the first place. It removes ONLY what it
      can prove is safe (clean AND merged AND aged AND idle, never
      ``--force``) and RECORDS any repo still over its cap. ``--all`` is
      well-defined here: it sweeps the local git repos declared as agents'
      ``spec.workdir``, so the command is correct as written.

    ``sac listen`` is DELIBERATELY NOT declared here, and adding it back
    would take the fleet's control plane down: scitex-dev derives the unit
    filename from the job name VERBATIM, so a ``sac.listen`` JobSpec
    materialises ``sac.listen.service`` while the unit that really runs is
    ``sac-listen.service`` (a HYPHEN) — a second ``Restart=always``
    supervisor fighting the first for 127.0.0.1:7878. The full argument,
    the PR that shipped on a premise already false, and the conditions
    under which it could ever be federated are recorded in
    ``docs/adr/0022-listen-is-not-a-jobspec.md``. The migration enforces it:
    ``_jobs._migrate.NEVER_TOUCH`` + ``assert_never_touches_listen``.

    Why ``sac.accounts-refresh`` is not ``--skip-active``: under the
    pre-2026-07-08 two-refresher model both the host timer and the
    in-container CLI redeemed the same single-use refresh_token, so
    skipping the active account was the race guard (2026-06-04 neurovista
    401 storm). Since 2026-07-08 agents bind the credential ``:ro`` and
    never refresh, making this timer the SOLE refresher — so
    ``--skip-active`` stopped guarding a race and instead starved the one
    account every agent uses, whose ~8h access_token then expired and
    401'd the whole fleet (2026-07-09/10 total stall).
    ``--sync-active-login`` keeps the operator's live session valid across
    the single-use refresh_token rotation.

    """
    from scitex_dev.jobs import JobSpec

    from ._specs_liveness import liveness_jobs

    return [
        JobSpec(
            # THE ONE NAME STILL ON THE LEGACY PREFIX, AND IT IS ON PURPOSE.
            # Every other job here was cut over to `scitex-agent-container-*`;
            # this one is HELD, with the reason recorded in
            # `_migrate._renames.RENAMES` (the SSoT for the cutover).
            #
            # A spec renamed AHEAD of its unit is the one shape that must not
            # ship. The live, enabled, actively-refreshing unit is
            # `sac.accounts-refresh.timer`; if this said
            # `scitex-agent-container-accounts-refresh` while that unit ran,
            # `sac dev timer status accounts-refresh` would resolve to a name
            # no unit carries and report the fleet's SOLE OAuth refresher as
            # ABSENT while it refreshes. A name that does not match the
            # convention yet is a PS-227 warning; a CLI that reports the
            # credential machinery as missing when it is healthy is an
            # incident. So the declared name tracks the DEPLOYED unit until
            # the supervised cutover renames both together.
            name="sac.accounts-refresh",
            schedule="0 */2 * * *",  # every 2h
            command=("sac accounts refresh --all --include-active --sync-active-login"),
            description=(
                "Headless OAuth access-token refresh for all stored Claude "
                "accounts including the active one (sole-refresher model), "
                "mirroring the rotation into the live ~/.claude login."
            ),
            # 2026-06-11 (lead msg c5212862): scitex_dev.jobs.JobSpec kind
            # taxonomy is {"service","timer","cron"} since scitex-dev #153.
            # ``sac.accounts-refresh`` is a periodic systemd --user timer
            # (token TTL ~7h, refresh every 2h) → ``kind="timer"`` with the
            # cadence carried by ``on_unit_active_sec`` below. The legacy
            # ``kind="systemd"`` is no longer accepted; it raises
            # ``ValueError`` at construction time and ``scitex-dev
            # ecosystem up`` silently drops sac's whole provider
            # (provider-isolated, WARN-only), leaving the OAuth refresh
            # unmanaged.
            kind="timer",
            on_boot_sec="15min",
            on_unit_active_sec="2h",
            timeout_sec=120,
        ),
        JobSpec(
            name="scitex-agent-container-accounts-keepalive",
            schedule="*/15 * * * *",  # every 15min (cron form; timer below)
            command=(
                "sac accounts keepalive --all "
                "--to ywata-note-win "
                "--to scitex-compute-03 "
                "--to scitex-compute-04"
            ),
            description=(
                "The DISTRIBUTION half of the single-refresher model, and "
                "the only thing keeping the access-only hosts alive. "
                "sac.accounts-refresh rotates the token on the ONE host that "
                "holds refresh material (scitex-nas-03 as of 2026-08-10); "
                "every other host holds an ACCESS-ONLY copy that nothing on "
                "that box can renew, so without this job those hosts simply "
                "expire and 401 within one access-token lifetime. COPIES the "
                "current token (never mints — minting rotates, which revokes "
                "the token running agents hold), refuses a payload carrying "
                "refresh material, refuses under 300s of validity, refuses "
                "to overwrite a valid remote credential with a dead one, "
                "backs up what it replaces, publishes 0600, and PROVES the "
                "far side answers HTTP 200. CONVERGENT: it compares "
                "fingerprints and rewrites a peer only when the master's "
                "token actually changed, so most runs are cheap verified "
                "no-ops. WORST-CASE FOLLOWER OUTAGE THE OPERATOR IS "
                "ACCEPTING AT THIS CADENCE: 15 minutes — the moment the "
                "master refreshes, every follower's copy is revoked, and "
                "they stay dead until the next tick converges them. Exits "
                "non-zero on any peer's failure. NOT armed by this "
                "declaration."
            ),
            kind="timer",
            # HOST PINNING IS NOT EXPRESSIBLE HERE. JobSpec has no host
            # field (name/kind/schedule/command/description/on_boot_sec/
            # on_unit_active_sec/timeout_sec/restart_policy/watchdog_sec/
            # venv), so WHERE this runs is decided by where the operator
            # installs it. It must run ONLY on the refresh holder. sac's
            # own mitigation is inside the verb: `--all` resolves to the
            # accounts THIS host holds refresh material for, and exits
            # non-zero when that set is empty — so a keepalive installed on
            # the wrong host fails loudly instead of pretending to work.
            #
            # 15min is a BOUND, not a guess. Measured 2026-08-10: Claude
            # Code refreshes only when the token is genuinely near expiry,
            # so the master's token changes ONCE in ~7h at an unpredictable
            # moment — and the instant it does, every follower's copy is
            # revoked and its agents 401. The tick therefore does not decide
            # when work happens (the fingerprint comparison does); it decides
            # only how long that revoked window lasts. 15min bounds the
            # follower outage to 15min; hourly would bound it to an hour.
            # The cost of the extra ticks is near zero because a converged
            # peer is verified, not rewritten.
            on_boot_sec="10min",
            on_unit_active_sec="15min",
            # Per peer: a handful of coreutils ssh ops plus ONE outbound
            # HTTPS verification from the peer (15s cap inside the probe).
            # 300s covers three peers including a slow one without ever
            # hanging forever. A pass killed here leaves the peer's previous
            # credential intact — nothing is published unverified.
            timeout_sec=300,
        ),
        JobSpec(
            name="scitex-agent-container-host-sync-check",
            schedule="0 * * * *",  # hourly (cron form; timer cadence below)
            command="sac host sync --check --all --alarm --exit-zero",
            description=(
                "Read-only drift check of every peer's sac checkout vs the "
                "centre; records each verdict in sac's own event log so the "
                "shout is DURABLE. Mutates nothing on any peer — never runs "
                "the fast-forward remedy (Stage 1). "
                "--exit-zero because FINDING drift is not this unit being "
                "unhealthy. MEASURED 2026-08-17: without it, drift exits 1 "
                "and undetermined exits 2, systemd recorded the unit "
                "`failed`, compute-04 went `degraded`, and the dotfiles sync "
                "installer read `is-system-running: degraded` as 'systemd "
                "absent' and silently refused to install its timer — so that "
                "host stopped receiving dotfiles sync altogether. The verdict "
                "still reaches its real readers: the printed report, the JSON "
                "`exit_code`, and the --alarm event-log record."
            ),
            kind="timer",
            # First check 10min after boot/login (peers reachable, listen
            # settled), then hourly. Drift is slow-moving relative to the
            # 2h token refresh, so hourly is ample and gentle on ssh.
            on_boot_sec="10min",
            on_unit_active_sec="1h",
            # Sequential per-ssh probe over every peer, each capped at the
            # verb's 120s default (an unreachable peer waits its ssh
            # connect-timeout). 600s comfortably covers a handful of peers
            # including a slow/unreachable one without ever hanging forever.
            timeout_sec=600,
        ),
        JobSpec(
            name="scitex-agent-container-worktree-gc",
            schedule="30 4 * * *",  # daily 04:30 (cron form; timer cadence below)
            command="sac worktree gc --apply --all",
            description=(
                "Daily git-worktree GC: removes only worktrees PROVEN safe "
                "(clean AND merged AND older than 24h AND not in use — never "
                "--force), prunes admin refs whose directory is already gone, "
                "and records any repo still over its worktree cap in sac's "
                "own event log (recorded as recovered when it drops back under). "
                "The permanent countermeasure to worktree sprawl."
            ),
            kind="timer",
            # Sprawl accumulates over days, not minutes, and the age gate is
            # 24h — so a daily pass is the natural cadence and a faster one
            # could not remove anything a daily one would miss. 20min after
            # boot keeps it clear of the login/auth settling window.
            on_boot_sec="20min",
            on_unit_active_sec="1d",
            # A pass is a handful of local `git` calls per worktree plus one
            # `gh pr list` per unmerged branch (the squash-merge leg). A repo
            # deep in sprawl with a slow/rate-limited gh is the worst case;
            # 900s covers the whole fleet's repos without ever hanging
            # forever. Every gh failure already degrades to KEEP, so a
            # timeout costs a skipped reap, never a wrong one.
            timeout_sec=900,
        ),
        JobSpec(
            name="scitex-agent-container-spartan-sif-bake",
            schedule="*/10 * * * *",  # every 10min (cron form; timer cadence below)
            command="sac image bake-remote --yes",
            description=(
                "10-minute SIF refresh with zero master CPU: bake sac-base + "
                "sac-scitex on the standing Spartan CPU lease (srun "
                "--overlap into the job resolved BY NAME, never sbatch), "
                "gate at build time (.def %post symbol gate) AND on the "
                "artifact (apptainer-exec symbol probe), keep-3 rotate the "
                "Spartan store, then PULL via rsync-over-ssh, re-verify "
                "here (sha256 + the same symbol probe on the received "
                "file) and only then atomically swap both live "
                "sac-<layer>.sif symlinks + keep-3 rotate locally. A "
                "failed leg leaves the live image untouched and exits "
                "non-zero; a source-unchanged run is a loud SKIPPED, not "
                "a transfer."
            ),
            kind="timer",
            # 10min: the image is a point-in-time snapshot of @develop, and at
            # our release rate a DAY-old SIF is mostly wrong. 30min was read
            # off the operator's 「最低でも30分に1回」 — but that was his FLOOR,
            # not his target (「なんで三十分に一回だけなの？」; 「例えば1分に1回焼いても
            # 全く問題ないです」), so the cadence is set to what he wants, not to
            # the minimum he would tolerate.
            #
            # Cheap by construction, which is what makes a 10min tick sane: a
            # source-unchanged run is a SKIPPED verdict (check a git ref, one
            # ssh round-trip, no transfer), so only a real @develop change ever
            # costs a bake. A bake takes 8-30min, so at */10 most ticks land
            # while one is still running — the script's `flock -n` makes those
            # exit "already-running" immediately instead of piling up, which is
            # exactly what that lock is for. The operator has separately
            # accepted overlap outright (the swap is an atomic symlink flip at
            # the end). Steady state: skip, skip, skip, … one real bake when
            # something changed, the rest of that window bouncing off the lock.
            on_boot_sec="30min",
            on_unit_active_sec="10min",
            # Two full bakes (base ~15-25min + scitex ~10-20min) plus a
            # multi-GB pull on a slow link fit comfortably; the per-leg
            # ssh timeout inside the command is 7200s, so 4h bounds the
            # whole chain without ever hanging forever.
            timeout_sec=14_400,
        ),
        JobSpec(
            name="scitex-agent-container-freshness-refresh",
            schedule="7 * * * *",  # hourly (cron form; timer cadence below)
            command="sac freshness refresh",
            description=(
                "Publishes the version-currency verdict to the cache that "
                "every `sac` invocation reads. Runs the real checks (PyPI, "
                "git tags, gh release runs, systemd running-vs-installed, "
                "symbol probes) via scitex-dev's `versioning` primitive and "
                "writes the result atomically. This is the half that pays "
                "the network cost, so the CLI hot path never does — without "
                "it the startup banner has nothing to read and stays "
                "permanently silent."
            ),
            kind="timer",
            # Hourly against the primitive's 24h cache TTL: 24 consecutive
            # misses before the banner falls silent, so a laptop that is shut
            # most of the day still has a trustworthy answer. Faster buys
            # nothing — releases are not more frequent than hourly — and each
            # pass makes real network calls.
            on_boot_sec="25min",
            on_unit_active_sec="1h",
            # Generous on purpose, matching the primitive's own 30s per-source
            # timeouts: a busy host must not be mistaken for a broken one, and
            # a manufactured UNKNOWN is exactly the failure mode. Nothing is
            # waiting on this run.
            timeout_sec=300,
        ),
        # The two AGENT-LIVENESS enforcers live together in
        # :mod:`._specs_liveness` — each one's scope is defined by what the
        # other covers (corpses vs live-but-wedged), so they are unreadable
        # apart. Spliced in HERE to preserve the historical order.
        *liveness_jobs(),
        JobSpec(
            name="scitex-agent-container-heal-agent-auth",
            schedule="*/10 * * * *",  # every 10min (cron form; timer cadence below)
            # ABSOLUTE by design, both tokens. `resolve_execstart` passes a
            # command whose head starts with "/" through VERBATIM, so this is
            # the only form that depends on neither the ambient PATH nor which
            # interpreter ran `ecosystem up`. A systemd --user unit gets a
            # MINIMAL PATH, so the venv python must be named outright: the
            # script's own `#!/usr/bin/env python3` would resolve to the SYSTEM
            # python under systemd, not the 3.11 venv the fleet runs on.
            command=(
                "/home/ywatanabe/.env-3.11/bin/python "
                "/home/ywatanabe/.scitex/agent-container/bin/auth-heal.py"
            ),
            description=(
                "Auth auto-heal for the fleet: scans each agent's session.jsonl "
                "TAIL for a CURRENT 401/authentication_error and restarts the "
                "agent so a fresh credential is re-mounted, plus the sibling "
                "TUI-pane scan (2-run-corroborated) for tmux agents whose "
                "transcript lives in the container overlay and is invisible to "
                "the session.jsonl glob. Rate-limited (per-agent debounce, boot "
                "grace, global cap/hour) and phones the operator instead of "
                "looping when a restart provably did not fix it. Declared as a "
                "JobSpec because its crontab line was swept by copy_crontab's "
                "full-manifest install. MUTUALLY EXCLUSIVE with "
                "sac.restart-login-expired-agents — enable exactly one."
            ),
            kind="timer",
            # Same taxonomy note as the jobs above: kind must be one of
            # {"service","timer","cron"} (scitex-dev #153); a wrong kind raises
            # at construction and `ecosystem up` then silently drops sac's WHOLE
            # provider.
            #
            # 10min preserves the incumbent cadence EXACTLY — not a guess: the
            # live `runtime/auth-heal.log` ticks 15:20 → 15:30 → 15:40 → 15:50
            # → 16:00 (2026-07-18), matching the `*/10` cron line. Migrating a
            # schedule is the wrong moment to also retune it; a cadence change
            # belongs in its own PR with its own argument.
            on_boot_sec="5min",
            on_unit_active_sec="10min",
            # `Persistent=true` is emitted by scitex-dev's timer renderer for
            # every kind="timer", so a window missed while the host was asleep
            # fires on resume — the property a crontab line does NOT have and a
            # laptop fleet needs most.
            #
            # 300s matches the siblings and comfortably outlives a real pass:
            # a no-op tick takes ~2-8s, and the worst observed pass (a TUI
            # restart at 15:30:04 settling by 15:32:08) ~2min. A pass killed
            # here is SAFE — state is persisted per restart, so the next tick
            # still honours the debounce for anything already bounced.
            timeout_sec=300,
        ),
    ]


__all__ = ["provide_jobs"]
