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


def provide_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """Return sac's federated scheduled jobs.

    Thirteen jobs today (the accounts, maintenance, liveness and
    reachability groups — one module each):

    * ``scitex-agent-container-a2a-reachability`` (``kind="timer"``) — the
      every-15-minutes probe of the CROSS-HOST a2a transport, from this host
      to every peer the fleet knows: ssh to the peer's alias, curl the
      peer's loopback listen with the peer's bearer — the same leg ``sac
      listen``'s forwarder takes for a cross-host send. Three-valued per
      host and never counts UNKNOWN as reachable. MEASURED 2026-09-02: two
      hosts with no ``config.yaml`` had been sending every cross-host
      message down a leg that cannot work in production, and nothing said
      so until a send was tried by hand. See :mod:`._specs_reachability`.

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

    * ``sac.resume-rate-limited-agents`` (``kind="timer"``) — the THIRD member
      of that family, and the shape the first two divide the fleet around
      without covering. A provider rate wall leaves the tmux session ALIVE, so
      fleet-reconcile hands off (correctly — there is no corpse), and the
      banner is not an auth banner, so the auth matcher excludes it (also
      correctly, and it says why at the exclusion: *a restart does not fix a
      rate wall*). Two right answers, and the agent stays stopped.

      INCIDENT 2026-08-28: a session limit stopped a set of agents at ~17:25
      UTC and lifted at 19:10 UTC; nothing resumed until the operator asked at
      20:56 UTC. This job reads the reset time out of the provider's OWN
      banner, HOLDS while the wall stands — so it structurally cannot spend a
      token against a live limit — and then CONTINUES the agent through the
      verified delivery path rather than restarting it, because the session
      and its whole context survived the wall. A wall whose reset it cannot
      parse is held and REPORTED, never guessed at.

      It keeps its OWN debounce ledger, like the other two keep theirs: the
      history file is a flat ``{agent: [epoch, ...]}`` with no subsystem key,
      so three enforcers sharing one file would consume each other's budget
      and race on one atomic write. Not gated: unlike its login-expired
      sibling there is no incumbent doing this job, because nothing was.

    * ``sac.heal-agent-auth`` — RETIRED 2026-08-20, and the succession is the
      reason. This spec declared the INCUMBENT healer
      (``~/.scitex/agent-container/bin/auth-heal.py``) alongside its own
      successor, with the note "MUTUALLY EXCLUSIVE WITH
      ``restart-login-expired-agents`` — enable exactly ONE". On the hosts the
      succession had already completed: the successor runs on all four
      (55 / 329 / 343 / 337 starts, exit 0 on three of them), while
      ``auth-heal.py`` and the interpreter it named are ABSENT from every host
      and from this repo, and its log file is gone.

      What was left was a declaration of a predecessor nobody has, which the
      supervisor faithfully tried to spawn every ten minutes and which
      faithfully failed — 535 times across the fleet by the morning it was
      measured (c01 28 x exit 127, c02 166, c03 172, c04 169 spawn failures).

      Repairing the path would have been the wrong repair: it would ENABLE the
      double-supervisor hazard this very bullet warned about, two restarters
      with independent debounce state on one fleet. The missing script was the
      only thing preventing that, and an accident should not be load-bearing.

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

    EVERY COMMAND IS SELF-BOUNDING; NONE DECLARES ``timeout_sec``
    ------------------------------------------------------------
    Each command starts with a literal ``/usr/bin/timeout <N> ``, because
    that is the only place a bound can survive. Since the supervisor
    redesign (operator policy 2026-06-14) ``scitex-dev ecosystem up``
    lowers every ``kind="timer"`` JobSpec onto the managed CRONTAB block,
    and a cron line is ``<schedule> <command> # marker`` and nothing else
    — it has no field a timeout could ride in.

    ``timeout_sec`` is DROPPED rather than kept alongside the prefix:
    scitex-dev's guard (``_up_timer_lowering.lowering_losses``) keys on the
    FIELD BEING SET, not on whether the command is really bounded, so a
    spec carrying both still counts as a dropped guarantee and still
    REFUSES the lowering — ``ecosystem up`` aborts for the whole ecosystem,
    measured, not assumed. Nothing is lost by dropping it: the bound moved
    into the command, where both rendering targets honour it. Previously it
    was a systemd-only promise that evaporated on cron, which is how
    ``fleet-reconcile`` piled up fourteen concurrent instances, the oldest
    45 minutes old (2026-07-18).

    Both tokens are ABSOLUTE, and the second one only became so after it
    cost a host. ``/usr/bin/timeout`` (GNU coreutils 9.4) is absolute so the
    bound depends on no PATH at all. The PAYLOAD used to stay PATH-relative
    (``sac …``) on the reasoning that sac's install path varies by host, so
    pinning one absolute ``sac`` would break the others — correct about the
    hazard, and answered rather than overridden by :mod:`._sac_bin`, which
    derives the path from ``sys.executable`` and is therefore per-host by
    construction with nothing hardcoded.

    THE CONSEQUENCE WAS DOCUMENTED HERE AND SCOPED TOO NARROWLY. This
    docstring used to say that a wrapper prefix stops ``resolve_execstart``
    absolutising the inner ``sac`` "should these specs ever be rendered as
    systemd units instead", and called it "not live today (``up`` writes cron,
    not units)". The hazard was real and the scope was wrong: the ecosystem
    supervisor spawns periodic jobs DIRECTLY through the same
    ``resolve_execstart``, so the payload was already unresolved on the live
    path. MEASURED 2026-08-20 on scitex-compute-01 — supervisor PATH without
    the venv, ALL TEN sac jobs at exit 127, zero successes, including the
    self-pull that would have delivered any fix. The other three hosts were
    healthy only because their supervisor inherited a PATH that happened to
    contain the venv.

    ``executable`` is a TEST SEAM, forwarded to each group and on to
    :func:`._sac_bin.sac_bin`. The fleet calls this with no argument and gets
    the console script beside the interpreter that imported the plugin, which
    is the whole point of the resolution above. A test passes a venv-shaped
    tree it built on disk, because otherwise the rendered payload depends on
    whether the RUNNING environment happens to have that console script — true
    in production, false under a PYTHONPATH-only CI run — and a guard over
    these specs would be asserting an environmental fact rather than a property
    of the specs. MEASURED 2026-08-20: that is exactly how the population guard
    went red in CI on three unrelated PRs while every host was behaving
    correctly.

    """
    from ._specs_accounts import accounts_jobs
    from ._specs_liveness import liveness_jobs
    from ._specs_maintenance import maintenance_jobs
    from ._specs_reachability import reachability_jobs

    # Each group is one operational concern a reader checks as a unit, and
    # each resolves its own absolute `sac` through :mod:`._sac_bin`. Spliced
    # in THIS order to preserve the historical order of the nine specs, which
    # `collect_cron_jobs` and every existing test still read positionally.
    return [
        *accounts_jobs(executable=executable),
        *maintenance_jobs(executable=executable),
        # The two AGENT-LIVENESS enforcers live together in
        # :mod:`._specs_liveness` — each one's scope is defined by what the
        # other covers (corpses vs live-but-wedged), so they are unreadable
        # apart.
        *liveness_jobs(executable=executable),
        # The cross-host a2a transport probe — appended LAST so every
        # positional reader above keeps its index.
        *reachability_jobs(executable=executable),
    ]


__all__ = ["provide_jobs"]
