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

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

#: Explicit override for WHICH ``sac`` a scheduled job runs.
SAC_BIN_ENV = "SAC_BIN"

#: The current release install on the fleet host. Five sac installs were
#: measured here on 2026-07-18 — 0.21.24 / 0.21.22 / 0.21.21 / 0.21.11 / none —
#: and WHICH ONE a bare ``sac`` resolves to depends on the invocation form
#: (login shell, systemd's minimal PATH, tmux, in-container). This one is the
#: release.
DEFAULT_SAC_BIN = "/home/ywatanabe/.env-3.11/bin/sac"


def sac_bin() -> str:
    """The ``sac`` a scheduled job should run. Resolved PER CALL, never cached.

    A bare ``sac`` in a JobSpec is NOT a stable reference: with five installs
    on one host at four different versions, "run sac" names whichever the unit's
    PATH happens to reach — so a timer can silently execute code months older
    than the one whose behaviour was tested. systemd units get a minimal PATH
    that is not the operator's login PATH, which makes the drift invisible
    rather than merely possible.

    Precedence: ``$SAC_BIN`` → the measured release path if it EXISTS →
    ``which sac`` → bare ``"sac"``. The last rung is a genuine fallback for a
    host that has none of the above, and is the only case where the ambiguity
    above is accepted (there is nothing better to name).

    Resolved per call, not at import: a module-level constant would bake the
    env var before a test could set it — the trap :mod:`._state.state_paths`
    documents having paid for.
    """
    override = os.environ.get(SAC_BIN_ENV)
    if override:
        return override
    if os.path.exists(DEFAULT_SAC_BIN):
        return DEFAULT_SAC_BIN
    return shutil.which("sac") or "sac"


def provide_jobs() -> "list[JobSpec]":
    """Return sac's federated scheduled jobs.

    Six jobs today. Note that ``sac.sync-pr-cards`` (the newest) uses an
    ABSOLUTE command path via :func:`sac_bin`, while the five older jobs still
    spell a bare ``sac``. That divergence is not an oversight — it is a
    deliberate non-drive-by: five sac installs at four versions were measured
    on this host on 2026-07-18, so the bare form is a real hazard for all of
    them, but changing the older five's commands is a behaviour change to
    running timers that belongs in its own PR with its own verification.

    * ``sac.sync-pr-cards`` (``kind="timer"``) — one scitex-todo card per OPEN
      pull request, keyed by PR number so it is idempotent, completed when the
      PR merges or closes. It builds NO nudge logic: scitex-todo's stale-active
      sweep already nudges the owner of any untouched open card, so a second
      nudger would only race it. sac owns the PR FACTS; scitex-todo owns the
      reminder. That split is why this job is narrow — the gap it fills is that
      an un-carded PR is invisible to the nudge rail entirely, which is how 35
      open PRs accumulated until 31 were force-closed by hand.

      TRI-STATE, and that is the point: if the PR list cannot be fetched the
      pass reports UNKNOWN and exits 2 rather than seeing an empty list and
      reporting a clean board. It also refuses to COMPLETE any card on such a
      pass — completion is inferred from a PR's absence, so a blind pass would
      otherwise complete every card at once.

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

    * ``sac.accounts-refresh`` (``kind="timer"``) — a headless OAuth
      access-token refresh for EVERY stored Claude account, including the
      active one (``--include-active``), mirroring the rotated token back
      into the live ``~/.claude`` login (``--sync-active-login``).

    * ``sac.host-sync-check`` (``kind="timer"``) — the READ-ONLY peer
      drift detector ``sac host sync --check --all``, run hourly with
      ``--alarm`` so each peer's verdict is routed to an idempotent
      scitex-todo card (upsert on drift/unknown, resolve on clean). This
      is what makes the Stage-0 detector (PR #690) actually RUN and be
      SEEN: shipped but scheduled nowhere, it was an inert alarm. The job
      mutates NOTHING on any peer — it never calls the fast-forward
      remedy (that is Stage 1). ``--alarm`` is gated to require
      ``--check`` in the CLI, so this scheduled command is read-only by
      construction.

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
      ``--force``) and cards any repo still over its cap. ``--all`` is
      well-defined here: it sweeps the local git repos declared as agents'
      ``spec.workdir``, so the command is correct as written.

    ``sac listen`` is DELIBERATELY NOT declared here, and adding it back
    would take the fleet's control plane down. scitex-dev derives a unit
    name from the job name VERBATIM (``scitex-todo.dashboard`` ->
    ``scitex-todo.dashboard.service``), so a ``sac.listen`` JobSpec
    materialises ``sac.listen.service`` — while the listen that actually
    runs on the host is ``sac-listen.service`` (a HYPHEN), hand-written
    2026-07-05 14:38, ``Restart=always``, with ``10-venv-path`` and
    ``20-hardening`` drop-ins. The two names differ by one character and
    systemd treats them as unrelated units, so ``scitex-dev service ensure
    sac.listen`` does not adopt the running supervisor — it installs a
    SECOND one. Two units, both ``Restart=always``, both running
    ``sac listen``, both binding 127.0.0.1:7878: they fight for the port
    forever, and every lost round destroys the in-memory Broker, which
    deafens EVERY agent's inbox at once.

    PR #543 declared it on the premise that ``sac listen`` "had NO
    SUPERVISOR". That premise was false by the time it merged — the
    hand-written unit was created the SAME DAY the PR was opened, and had
    been supervising listen for nine days (``NRestarts=0``). The PR was
    obsolete on arrival and nobody re-checked before merging it.

    If this is ever federated, it must be named ``sac-listen`` (hyphen) so
    the derived unit is the one that already exists — and even then,
    ``ensure`` must be shown to ADOPT the running unit rather than
    overwrite its drop-ins. Do not re-add it without measuring that.

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

    The clew incident (``clew-incident-sac-host-listen-down``, 2026-07-05)
    that motivated federating listen was ALREADY fixed on the day it
    happened, by the hand-written ``sac-listen.service`` above — not by a
    JobSpec. The fragile ``sac-listen-watch.sh`` ``*/2`` cron it replaced
    is gone. Re-federating it does not fix that incident again; it only
    adds a second supervisor to fight the first.
    """
    from scitex_dev.jobs import JobSpec

    return [
        JobSpec(
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
            name="sac.host-sync-check",
            schedule="0 * * * *",  # hourly (cron form; timer cadence below)
            command="sac host sync --check --all --alarm",
            description=(
                "Read-only drift check of every peer's sac checkout vs the "
                "centre; routes each verdict to an idempotent scitex-todo "
                "card (upsert on drift/unknown, resolve on clean) so the "
                "shout is SEEN on the board. Mutates nothing on any peer — "
                "never runs the fast-forward remedy (Stage 1)."
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
            name="sac.worktree-gc",
            schedule="30 4 * * *",  # daily 04:30 (cron form; timer cadence below)
            command="sac worktree gc --apply --all",
            description=(
                "Daily git-worktree GC: removes only worktrees PROVEN safe "
                "(clean AND merged AND older than 24h AND not in use — never "
                "--force), prunes admin refs whose directory is already gone, "
                "and upserts an idempotent scitex-todo card for any repo still "
                "over its worktree cap (resolved when it drops back under). "
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
            name="sac.fleet-reconcile",
            schedule="*/5 * * * *",  # every 5min (cron form; timer cadence below)
            command="sac agents reconcile --apply",
            description=(
                "The enforcer of 'should be running => is running'. Restarts "
                "agents whose tmux session is GONE while their spec asks to be "
                "kept running AND nothing recorded a deliberate stop. Only ever "
                "touches a CORPSE (no session => no context to lose); never a "
                "live-but-wedged agent (auth-heal owns those) and never a "
                "deliberately-stopped one. Rate-limited (30min/agent debounce, "
                "<=2/agent/hour, <=10/pass); an agent it cannot recover gets a "
                "scitex-todo card instead of an endless bounce."
            ),
            kind="timer",
            # THIS JOB IS THE MECHANISM, not an optimisation. `restart.policy`
            # in ~93 specs is dead code — `_lifecycle/_start.py` launches the
            # loop that reads it on a `daemon=True` thread and then returns,
            # and `sac agents start` is a short-lived CLI, so the supervisor
            # dies with the process that promised it. Nothing else owns fleet
            # liveness: `sac listen`'s reconciler only alarms on stuck CARDS.
            # An OAuth rotation killed 33 agents and they stayed dead until the
            # operator noticed by chance. Unschedule this and that returns.
            #
            # 5min: the window an agent stays dead. A pass that finds nothing
            # (the normal case) is one batched `tmux list-sessions` plus a spec
            # read each — cheap enough to run often.
            on_boot_sec="5min",
            on_unit_active_sec="5min",
            # A no-op pass takes ~seconds; this bounds the pathological one
            # (`--limit` restarts, each a stop+settle+start). A pass killed at
            # this timeout is SAFE: the restart history is persisted per
            # restart, not at the end, so the next tick still honours the
            # debounce for anything already bounced.
            timeout_sec=300,
        ),
        JobSpec(
            name="sac.restart-login-expired-agents",
            schedule="*/5 * * * *",  # every 5min (cron form; timer cadence below)
            command="sac agents restart-login-expired --apply",
            description=(
                "Restarts LIVE agents wedged behind a frozen 'Login expired' "
                "banner (auth-dead but tmux-alive) — the half fleet-reconcile "
                "leaves alone. Detection is READ-ONLY + 2-run-corroborated (a "
                "banner that moved between the two captures = working, never "
                "restarted); the restart runs through the pool-loading start "
                "path (cannot strip CCT tokens) and is rate-limited (30min/agent "
                "debounce, <=2/agent/hour, <=10/pass); an agent still wedged "
                "after the cap gets a scitex-todo card, not an endless bounce. "
                "DEPLOY GATE: do NOT enable until the host's auth-heal.py "
                "scan_tui cron is retired (double-supervisor risk)."
            ),
            # Same taxonomy note as the jobs above: kind must be one of
            # {"service","timer","cron"} (scitex-dev #153); a periodic
            # systemd --user timer is ``kind="timer"`` with the cadence in
            # ``on_unit_active_sec``. A wrong kind raises at construction and
            # ``ecosystem up`` then silently drops sac's WHOLE provider.
            kind="timer",
            # 5min matched to fleet-reconcile so the two enforcers sweep on the
            # same beat rather than harmonising into one; it is the window a
            # wedged agent stays wedged. A no-op pass is one `tmux list-sessions`
            # plus two pane captures ~4s apart.
            on_boot_sec="5min",
            on_unit_active_sec="5min",
            # Mirrors fleet-reconcile: bounds the pathological pass (`--limit`
            # restarts, each a stop+settle+start) plus the ~4s capture interval.
            # A pass killed here is SAFE — history is persisted per restart, so
            # the next tick still honours the debounce for anything bounced.
            timeout_sec=300,
        ),
        JobSpec(
            name="sac.sync-pr-cards",
            schedule="*/30 * * * *",  # every 30min (cron form; timer cadence below)
            command=f"{sac_bin()} pr sync-cards --apply",
            description=(
                "Upserts ONE scitex-todo card per OPEN pull request (title, "
                "author, age, draft state, CI status), keyed by PR number so it "
                "is idempotent, and completes the card when the PR merges or "
                "closes. Feeds FACTS only — scitex-todo's stale-active sweep "
                "already owns nudging, so this deliberately builds none. "
                "TRI-STATE: if the PR list cannot be read (gh unauthenticated / "
                "offline / rate-limited) it reports UNKNOWN and exits 2, and "
                "touches no card — an unreadable backlog is NOT an empty one."
            ),
            kind="timer",
            # THE POINT OF THIS JOB is that nobody has to remember. The
            # operator's diagnosis of the 2026-07-18 backlog was three failures
            # —「カードになってないか追跡できてない、催促できてないのも悪い」—
            # and the FIRST is the one nothing owned: a PR with no card is
            # invisible to the fleet, so the nudge rail that already exists has
            # nothing to nudge about. 35 PRs accumulated that way and 31 were
            # force-closed BY HAND. Unschedule this and that returns.
            #
            # 30min: the window a new PR stays untracked. The cadence only has
            # to beat the shelf life (days) by a wide margin, and a no-op pass
            # is one `gh pr list` per repo — so this is already generous while
            # staying gentle on the GitHub API.
            on_boot_sec="10min",
            on_unit_active_sec="30min",
            # One `gh pr list` per repo plus one card upsert per open PR. The
            # pathological pass is a large backlog on a slow API. A pass killed
            # at this timeout is SAFE: every card write is independent and
            # idempotent, so the next tick simply redoes what it missed — and
            # card COMPLETION is gated on a readable fetch, so a truncated pass
            # can never mass-complete cards.
            timeout_sec=600,
        ),
        # NO `sac.close-expired-prs` JOB — deliberately, and this absence is
        # load-bearing.
        #
        # The 3-day PR shelf life is a FLEET-WIDE rule (operator, 2026-07-18:
        # 「3日ルールは全てのレポジトリで共通です」) that scitex-dev owns as a
        # shared primitive, under the standing rule that dev holds primitives
        # and leaves consume them. sac implementing its own staleness decision
        # would FORK that primitive — two repos hand-rolling "what does stale
        # mean" is the exact failure mode being corrected, and the fleet's
        # answer would then depend on which tool you asked.
        #
        # An earlier draft of this branch DID declare the timer and implement
        # the logic. Both were removed rather than left as a placeholder: a
        # scheduled job calling a primitive that does not exist is a red timer,
        # and "we'll swap it for the real one later" is how this host ended up
        # with five differently-versioned sac installs.
        #
        # See `_prlifecycle/_expiry_seam.py` for the consumption contract sac
        # will hold dev's primitive to (dry-run default; the intent-registry
        # write must SUCCEED before any close proceeds; scheduled
        # materialisation; tri-state verdict). Tracking card:
        # `sac-pr-expiry-consume-dev-primitive`, blocked on scitex-dev.
    ]


__all__ = ["provide_jobs"]
