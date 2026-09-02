"""The housekeeping beat: drift, worktrees, images, freshness.

Split out of :mod:`._jobs_plugin` (at the per-file cap), following the
convention :mod:`._specs_liveness` established for the same reason.

Unlike the accounts group these five are INDEPENDENT — none depends on
another's output and any one can be disabled without breaking the rest.
What makes them one file is the shared property that every one of them
REPORTS rather than repairs, so a finding is not the unit being
unhealthy. That distinction is why ``host-sync-check`` carries
``--exit-zero``; see its comment for the measurement that forced it.

``timer-liveness-check`` is the one that reports on the SCHEDULER rather than
on the fleet, and it is deliberately in this group rather than in
:mod:`._specs_liveness`: those three restart AGENTS, this one only reports,
and grouping by "reports rather than repairs" is what makes either file
readable. It is also, unavoidably, a check that can be killed by the very
fault it detects — a dead timer cannot report that timers are dead. That is
not a reason to skip it. Under the ecosystem supervisor's PeriodicRunner the
cadence comes from ``on_unit_active_sec`` and no systemd timer is involved at
all, so the check runs on a rail the fault cannot reach; on a host still
carrying the retired per-leaf units it would go silent WITH them, and a
detector that goes quiet in the same breath as the thing it watches is still
better than the five days of confident green that preceded it.

CADENCE, HONESTLY: the permanent fix for the fault above is a wall-clock
trigger — ``OnCalendar=*:0/5`` with ``Persistent=true``, which cannot land in
the past-forever state and catches up a missed run. It is NOT applied to the
recovery-critical specs here or in :mod:`._specs_liveness`, and the reason is
measured, not stylistic. ``JobSpec.on_calendar`` exists in scitex-dev 0.57.0
and its SYSTEMD renderer emits it (``jobs/_systemd.py``), but neither live
rail reads it: the ecosystem supervisor's cadence resolver
(``_supervisor/_schedule.cadence_sec``) consults ``on_unit_active_sec`` then
``schedule`` and never ``on_calendar``, and ``ecosystem up``'s cron lowering
(``_up_timer_losses.lowering_losses``) counts a set ``on_calendar`` as a
DROPPED guarantee and REFUSES the whole lowering. So declaring one here would
change nothing about when these jobs run and would break the lowering for
every sac job at once. The missing half belongs to scitex-dev, and it is
exactly one thing: ``cadence_sec`` (and the periodic runner behind it) has no
``on_calendar`` branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["maintenance_jobs"]


def maintenance_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """sac's housekeeping JobSpecs, in their historical order.

    ``executable`` is the same test seam :func:`._sac_bin.sac_bin` exposes,
    threaded through so a test can resolve the payload against a venv-shaped
    tree it built on disk. Without it the rendered command depends on whether
    the RUNNING environment happens to have a ``sac`` console script beside
    its interpreter — true in production, false under a PYTHONPATH-only CI
    run — and a population guard over these specs would then assert an
    environmental fact rather than a property of the specs.
    """
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin` for the measurement.
    sac = sac_bin(executable=executable)

    return [
        JobSpec(
            name="scitex-agent-container-host-sync-check",
            schedule="0 * * * *",  # hourly (cron form; timer cadence below)
            # SELF-BOUNDING (600s). Sequential per-ssh probe over every peer,
            # each capped at the verb's 120s default (an unreachable peer
            # waits its ssh connect-timeout). 600s comfortably covers a
            # handful of peers including a slow/unreachable one without ever
            # hanging forever.
            command=(
                "/usr/bin/timeout 600 "
                f"{sac} host sync --check --all --alarm --exit-zero"
            ),
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
        ),
        JobSpec(
            name="scitex-agent-container-worktree-gc",
            schedule="30 4 * * *",  # daily 04:30 (cron form; timer cadence below)
            # SELF-BOUNDING (900s). A pass is a handful of local `git` calls
            # per worktree plus one `gh pr list` per unmerged branch (the
            # squash-merge leg). A repo deep in sprawl with a slow/
            # rate-limited gh is the worst case; 900s covers the whole
            # fleet's repos without ever hanging forever. Every gh failure
            # already degrades to KEEP, so a timeout costs a skipped reap,
            # never a wrong one.
            command=f"/usr/bin/timeout 900 {sac} worktree gc --apply --all --exit-zero",
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
        ),
        JobSpec(
            name="scitex-agent-container-spartan-sif-bake",
            schedule="*/10 * * * *",  # every 10min (cron form; timer cadence below)
            # SELF-BOUNDING (14400s = 4h), and the one where the bound
            # matters most: at a */10 cadence an UNBOUNDED bake outlives its
            # own tick by design, so a wedged run would pile up against every
            # later one. Two full bakes (base ~15-25min + scitex ~10-20min)
            # plus a multi-GB pull on a slow link fit comfortably; the
            # per-leg ssh timeout inside the command is 7200s, so 4h bounds
            # the whole chain without ever killing a legitimate run.
            command=f"/usr/bin/timeout 14400 {sac} image bake-remote --yes",
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
        ),
        JobSpec(
            name="scitex-agent-container-freshness-refresh",
            schedule="7 * * * *",  # hourly (cron form; timer cadence below)
            # SELF-BOUNDING (300s). Generous on purpose, matching the
            # primitive's own 30s per-source timeouts: a busy host must not
            # be mistaken for a broken one, and a manufactured UNKNOWN is
            # exactly the failure mode. Nothing is waiting on this run.
            command=f"/usr/bin/timeout 300 {sac} freshness refresh",
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
        ),
        JobSpec(
            name="scitex-agent-container-timer-liveness-check",
            schedule="23 * * * *",  # hourly (cron form; timer cadence below)
            # SELF-BOUNDING (120s). A pass is one `systemctl --user
            # list-unit-files` plus one `systemctl --user show` per enabled
            # timer — ~22 units on a fleet host, milliseconds each. 120s is
            # generous enough that a loaded host is never mistaken for a
            # broken one, which matters here: a timed-out pass reports
            # UNKNOWN, and a manufactured UNKNOWN is exactly the reading this
            # check exists to refuse to manufacture.
            command=f"/usr/bin/timeout 120 {sac} doctor --timers --strict",
            description=(
                "Asks the one question every other systemd instrument answers "
                "WRONG: will each ENABLED --user timer ever fire again? A timer "
                "built from OnBootSec+OnUnitActiveSec alone stops re-arming the "
                "first time its service misses a period (the next monotonic "
                "elapse lands in the past, systemd does not fire monotonic "
                "timers retroactively, and Persistent=true applies only to "
                "OnCalendar). MEASURED 2026-09-02 on scitex-compute-04: six "
                "enabled timers dead — the agent auth-heal sweep among them, "
                "silent for FIVE DAYS while a wedged agent went unrestarted — "
                "and is-enabled=enabled, is-active=active and Result=success "
                "throughout. Only NextElapseUSecMonotonic=infinity said "
                "otherwise, and nothing was reading it. READ-ONLY: it never "
                "starts, re-arms, enables or removes a unit; the remedy is "
                "printed for a human, because for an ORPHAN of the retired "
                "per-leaf timer lowering the correct remedy is REMOVAL and "
                "re-arming it would install a second scheduler. --strict so an "
                "UNREADABLE timer is loud too: `systemctl --user show` needs a "
                "user bus, so a pass with no vantage learns nothing and must "
                "not be recorded as a clean host."
            ),
            kind="timer",
            # Hourly, and the cadence is bounded from BOTH sides. A timer dies
            # the moment its service misses one period, so the fleet's fastest
            # job (5min) can be dead 55 minutes before this notices — and that
            # is still 5 days better than the operator noticing. Faster buys
            # little: the failure is measured in days of silence, and each pass
            # spawns one subprocess per enabled unit.
            #
            # 40min after boot on purpose, and it is not arbitrary: a REBOOT
            # RE-ARMS OnBootSec, so every timer on a just-booted host reads
            # armed regardless of how broken its rendering is. Checking at
            # boot would report the most reassuring possible answer at the
            # exact moment it is least informative. Uptime is the risk factor.
            on_boot_sec="40min",
            on_unit_active_sec="1h",
        ),
    ]
