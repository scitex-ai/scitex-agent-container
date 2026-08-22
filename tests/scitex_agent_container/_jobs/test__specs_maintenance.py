"""The housekeeping JobSpecs: drift, worktrees, images.

Split out of ``test__jobs_plugin.py`` (at the per-file cap), mirroring the
split of :mod:`scitex_agent_container._jobs._specs_maintenance` on the source
side.

These three are INDEPENDENT of each other — none consumes another's output —
and what they share is that every one of them REPORTS rather than repairs. The
assertions here protect that: ``host-sync-check`` must stay read-only and must
never acquire the mutating remedy, ``worktree-gc`` must keep ``--apply`` and
its every-repo sweep, and ``spartan-sif-bake`` must stay in its confirmed form.
"""

from __future__ import annotations


import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)


from ._jobspec_helpers import _job, _split_command  # noqa: E402


def test_host_sync_check_job_name_is_package_prefixed() -> None:
    # Arrange — the drift-alarm timer that makes the Stage-0 detector run.
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert
    assert job.name == "scitex-agent-container-host-sync-check"

def test_host_sync_check_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer (hourly), so kind="timer".
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert
    assert job.kind == "timer"

def test_host_sync_check_command_is_the_readonly_check() -> None:
    # Arrange — the scheduled command MUST carry --check. A timer that could
    # fast-forward a peer unattended is Stage 1, explicitly out of scope.
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert
    assert "--check" in job.command

def test_host_sync_check_command_routes_to_a_seen_card() -> None:
    # Arrange — --alarm is what turns the exit code into a SEEN board card
    # instead of a journald line nobody reads.
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert
    assert "--alarm" in job.command

def test_host_sync_check_command_never_runs_the_mutating_remedy() -> None:
    # Arrange — belt-and-braces: the exact mutating form `sac host sync
    # <peer>` (no --check) must never be what this timer runs. The command
    # is the read-only detector, full stop.
    #
    # `--exit-zero` was added 2026-08-17 and does NOT weaken that intent: it
    # touches only this process's exit status, never a peer. It is here
    # because the tri-state verdict (1 = drift found, 2 = undetermined) was
    # being read by systemd as unit failure, which put the host into
    # `degraded`, which made the dotfiles sync installer conclude systemd
    # was absent and silently stop installing its timer.
    #
    # Kept as EXACT EQUALITY on purpose. This assertion is the reason that
    # change could not land unnoticed — a substring check would have let it
    # through silently, and the next edit to this command deserves the same
    # scrutiny. Update the literal deliberately; do not relax the operator.
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert — the command is precisely the read-only check+alarm form.
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 600", "host sync --check --all --alarm --exit-zero")

def test_host_sync_check_cadence_is_hourly() -> None:
    # Arrange — drift is slow-moving; hourly is ample and gentle on ssh.
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert
    assert job.on_unit_active_sec == "1h"

def test_worktree_gc_job_name_is_package_prefixed() -> None:
    # Arrange — the daily GC that makes the worktree-sprawl countermeasure
    # PERIODIC. A GC nobody schedules is a script, not a countermeasure —
    # which is exactly how one repo reached 105 worktrees.
    # Act
    job = _job("scitex-agent-container-worktree-gc")
    # Assert
    assert job.name == "scitex-agent-container-worktree-gc"

def test_worktree_gc_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer (daily), so kind="timer".
    # A bad kind makes `ecosystem up` silently drop sac's WHOLE provider.
    # Act
    job = _job("scitex-agent-container-worktree-gc")
    # Assert
    assert job.kind == "timer"

def test_worktree_gc_command_is_the_apply_form() -> None:
    # Arrange — the scheduled job must ACT, not just report: a timer that
    # only dry-runs would print a nightly report nobody reads while the
    # sprawl kept growing. The safety lives in the predicate, not in
    # withholding --apply.
    # Act
    job = _job("scitex-agent-container-worktree-gc")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == (
        "/usr/bin/timeout 900",
        # --exit-zero is LOAD-BEARING and pinned here on purpose. Exit 1 from
        # this verb means "a repo is over cap, a human decides" — a finding,
        # not ill health. Without the flag the supervisor records the job as
        # failed, and the rule for retiring its duplicate systemd timer was
        # "the supervisor has a recorded exit-0", which this job could then
        # never produce. Dropping the flag silently re-creates that deadlock.
        "worktree gc --apply --all --exit-zero",
    )

def test_worktree_gc_command_sweeps_every_declared_repo() -> None:
    # Arrange — --all is only correct because it HAS a clean source (every
    # agent spec.workdir that is a local git repo toplevel). If that source
    # ever disappears, this command silently sweeps nothing.
    # Act
    job = _job("scitex-agent-container-worktree-gc")
    # Assert
    assert "--all" in job.command

def test_worktree_gc_cadence_is_daily() -> None:
    # Arrange — sprawl accumulates over days and the age gate is 24h, so a
    # faster pass could not remove anything a daily one would miss.
    # Act
    job = _job("scitex-agent-container-worktree-gc")
    # Assert
    assert job.on_unit_active_sec == "1d"

def test_spartan_sif_bake_job_name_is_package_prefixed() -> None:
    # Arrange — the daily remote SIF bake (operator directive 2026-07-17:
    # bake on Spartan, rsync to the master, zero master CPU).
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    assert job.name == "scitex-agent-container-spartan-sif-bake"

def test_spartan_sif_bake_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer (*/10), so kind="timer".
    # A bad kind makes `ecosystem up` silently drop sac's WHOLE provider.
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    assert job.kind == "timer"

def test_spartan_sif_bake_command_is_the_confirmed_form() -> None:
    # Arrange — `sac image bake-remote` REFUSES to run without --yes
    # (exit 2), mirroring `sac image build`'s non-interactive gate. A
    # scheduled command missing --yes would fail every single night —
    # a timer that fires and does nothing, the inert-feature shape.
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 14400", "image bake-remote --yes")

def test_spartan_sif_bake_cadence_is_every_10_minutes() -> None:
    # Arrange — the SIF is a point-in-time snapshot of @develop, and at our
    # release rate a day-old one is mostly wrong. 30min was the operator's
    # stated FLOOR (「最低でも30分に1回」), not his target — he asked why it was
    # only every 30 and said even 1min would be fine — so this pins the
    # cadence he wants. skip-if-unchanged keeps a no-change tick at one ssh
    # round-trip instead of a multi-GB transfer, and the script's `flock -n`
    # single-flights the overlaps a 10min interval necessarily causes.
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    assert job.on_unit_active_sec == "10min"

def test_spartan_sif_bake_timeout_outlives_two_bakes_and_a_pull() -> None:
    # Arrange — two full bakes (base + scitex) plus a multi-GB pull on a
    # slow link must fit; the per-leg ssh timeout is 7200s, so the cap
    # must exceed the worst legitimate chain or the timer kills its
    # own successful runs.
    #
    # This job needs a REAL bound more than any sibling: at a */10 cadence
    # an unbounded bake outlives its own tick by construction, so a wedged
    # run piles up against every later one. The bound therefore has to be
    # in the command — cron, the surface this is deployed to, cannot carry
    # `timeout_sec` at all.
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    assert job.command.startswith("/usr/bin/timeout 14400 ")
