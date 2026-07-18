"""Tests for the ``scitex_dev.jobs`` provider (``_jobs_plugin``).

Verifies the JobSpecs sac registers under the ``scitex_dev.jobs``
entry-point group match the federated contract:

* ``sac.accounts-refresh`` — a periodic systemd timer job that runs
  ``--all --include-active --sync-active-login`` every 2h (the SOLE
  refresher; see the ``--skip-active`` note below).
* ``sac.host-sync-check`` — the hourly READ-ONLY peer drift detector.
* ``sac.worktree-gc`` — the daily worktree GC.
* ``sac.fleet-reconcile`` — the 5-minute enforcer of "should be running ⇒
  is running". Its pins matter more than most: ``restart.policy`` in ~93
  specs is DEAD CODE without this timer (the loop that reads it runs on a
  daemon thread inside the short-lived ``sac agents start`` CLI and dies
  with it), so a wrong ``kind`` — which makes ``ecosystem up`` silently
  drop sac's whole provider — or a command that lost ``--apply`` would put
  the fleet back to dying unnoticed.

``sac listen`` is deliberately NOT federated, and one test below PINS its
absence: scitex-dev derives the unit name verbatim, so a ``sac.listen``
JobSpec installs ``sac.listen.service`` ALONGSIDE the ``sac-listen.service``
(hyphen) that already supervises the daemon — two units, both
``Restart=always``, both binding 127.0.0.1:7878, fighting for the port and
destroying the in-memory Broker (which deafens every agent's inbox) on each
lost round. See ``_jobs_plugin.provide_jobs`` for the full reasoning.

Skipped cleanly if the installed scitex-dev predates ``scitex_dev.jobs``
(PyPI lag) — the entry-point registration is install-time metadata and
the provider import is lazy, so an old scitex-dev must not fail CI here.
"""

from __future__ import annotations

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs_plugin import provide_jobs  # noqa: E402


def _job(name: str):
    (match,) = [j for j in provide_jobs() if j.name == name]
    return match


@pytest.fixture
def sac_bin_override():
    """Set a REAL ``$SAC_BIN`` for the duration of one test, then restore it.

    A genuine environment mutation, not a patched lookup: the production code
    reads ``os.environ`` itself, so this exercises the real resolution path.
    Teardown restores the prior value exactly (including its absence), so the
    suite cannot leak a fake sac path into any later test.
    """
    import os

    key = "SAC_BIN"
    sentinel = "/opt/elsewhere/bin/sac"
    previous = os.environ.get(key)
    os.environ[key] = sentinel
    try:
        yield sentinel
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def test_provider_returns_six_jobs() -> None:
    # Arrange — call the registered provider. Six: accounts-refresh, the
    # host-sync-check drift alarm, the daily worktree GC, the fleet-reconcile
    # enforcer (dead/no-session corpses), the restart-login-expired-agents
    # timer (live-session-but-auth-dead agents), and sync-pr-cards (one board
    # card per open PR). `sac listen` is still NOT federated (see the module
    # docstring and the absence-pin below).
    # Act
    jobs = provide_jobs()
    # Assert
    assert len(jobs) == 6


def test_provider_jobs_are_real_jobspecs() -> None:
    # Arrange — call the registered provider.
    # Act
    jobs = provide_jobs()
    # Assert — every entry is the canonical contract type, not a look-alike.
    assert all(isinstance(job, jobs_mod.JobSpec) for job in jobs)


def test_provider_job_name_is_package_prefixed() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.name == "sac.accounts-refresh"


def test_provider_job_command_includes_active_account() -> None:
    # Arrange — call the registered provider. This assertion previously
    # pinned ``--skip-active``, which was correct only under the
    # pre-2026-07-08 TWO-refresher model (host timer + in-container CLI
    # racing on one single-use refresh_token). Agents now bind the
    # credential ``:ro`` and never refresh, so this timer is the SOLE
    # refresher: skipping the active account starved the one account the
    # whole fleet uses until its ~8h access_token expired (2026-07-09/10
    # total stall). Do NOT revert to --skip-active.
    # Act — by NAME, not by index: this provider now returns two jobs, so
    # provide_jobs()[0] would silently start asserting against the wrong
    # JobSpec the day the list order changes.
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.command == (
        "sac accounts refresh --all --include-active --sync-active-login"
    )


def test_provider_job_command_never_skips_active() -> None:
    # Arrange — a belt-and-braces guard: --skip-active must never
    # reappear in the sole-refresher timer, however the command is spelled.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert "--skip-active" not in job.command


def test_provider_job_kind_is_timer() -> None:
    # Arrange — call the registered provider. The legacy ``kind=
    # "systemd"`` is no longer accepted by JobSpec.validate() since
    # scitex-dev #153; ``sac.accounts-refresh`` is a periodic
    # systemd --user timer (token TTL ~7h, refresh every 2h) so the
    # canonical kind is ``"timer"`` (lead msg c5212862, 2026-06-11).
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.kind == "timer"


def test_every_provided_job_uses_an_allowed_kind() -> None:
    # Arrange — defensive: even when a new entry is added without a
    # paired pinning test, the taxonomy gate still fires here so the
    # whole provider is never silently dropped by ``ecosystem up``.
    # Act
    kinds = {j.kind for j in provide_jobs()}
    # Assert — JobSpec.ALLOWED_KINDS is the canonical taxonomy.
    assert kinds <= jobs_mod.ALLOWED_KINDS


def test_provider_job_cadence_is_two_hours() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("sac.accounts-refresh")
    # Assert
    assert job.on_unit_active_sec == "2h"


def test_provider_does_not_federate_listen_it_would_duplicate_the_supervisor() -> None:
    # Arrange — `sac listen` must NOT be declared as a JobSpec, and this
    # test exists to keep it that way.
    #
    # scitex-dev derives the unit name from the job name VERBATIM
    # (`scitex-todo.dashboard` -> `scitex-todo.dashboard.service`), so a
    # `sac.listen` JobSpec materialises `sac.listen.service`. The listen
    # that actually runs is `sac-listen.service` — a HYPHEN — hand-written
    # 2026-07-05, Restart=always, NRestarts=0. systemd treats the two names
    # as unrelated units, so `scitex-dev service ensure sac.listen` does not
    # adopt the running supervisor: it installs a SECOND one. Two units,
    # both Restart=always, both running `sac listen`, both binding
    # 127.0.0.1:7878 — they fight for the port forever, and every lost round
    # destroys the in-memory Broker, deafening EVERY agent's inbox at once.
    #
    # PR #543 declared it because listen "had NO SUPERVISOR". That premise
    # was already false when it merged: the hand-written unit was created
    # the same day the PR was opened and had supervised listen for 9 days.
    # Act
    names = [spec.name for spec in provide_jobs()]
    # Assert
    assert "sac.listen" not in names


def test_host_sync_check_job_name_is_package_prefixed() -> None:
    # Arrange — the drift-alarm timer that makes the Stage-0 detector run.
    # Act
    job = _job("sac.host-sync-check")
    # Assert
    assert job.name == "sac.host-sync-check"


def test_host_sync_check_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer (hourly), so kind="timer".
    # Act
    job = _job("sac.host-sync-check")
    # Assert
    assert job.kind == "timer"


def test_host_sync_check_command_is_the_readonly_check() -> None:
    # Arrange — the scheduled command MUST carry --check. A timer that could
    # fast-forward a peer unattended is Stage 1, explicitly out of scope.
    # Act
    job = _job("sac.host-sync-check")
    # Assert
    assert "--check" in job.command


def test_host_sync_check_command_routes_to_a_seen_card() -> None:
    # Arrange — --alarm is what turns the exit code into a SEEN board card
    # instead of a journald line nobody reads.
    # Act
    job = _job("sac.host-sync-check")
    # Assert
    assert "--alarm" in job.command


def test_host_sync_check_command_never_runs_the_mutating_remedy() -> None:
    # Arrange — belt-and-braces: the exact mutating form `sac host sync
    # <peer>` (no --check) must never be what this timer runs. The command
    # is the read-only detector, full stop.
    # Act
    job = _job("sac.host-sync-check")
    # Assert — the command is precisely the read-only check+alarm form.
    assert job.command == "sac host sync --check --all --alarm"


def test_host_sync_check_cadence_is_hourly() -> None:
    # Arrange — drift is slow-moving; hourly is ample and gentle on ssh.
    # Act
    job = _job("sac.host-sync-check")
    # Assert
    assert job.on_unit_active_sec == "1h"


def test_worktree_gc_job_name_is_package_prefixed() -> None:
    # Arrange — the daily GC that makes the worktree-sprawl countermeasure
    # PERIODIC. A GC nobody schedules is a script, not a countermeasure —
    # which is exactly how one repo reached 105 worktrees.
    # Act
    job = _job("sac.worktree-gc")
    # Assert
    assert job.name == "sac.worktree-gc"


def test_worktree_gc_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer (daily), so kind="timer".
    # A bad kind makes `ecosystem up` silently drop sac's WHOLE provider.
    # Act
    job = _job("sac.worktree-gc")
    # Assert
    assert job.kind == "timer"


def test_worktree_gc_command_is_the_apply_form() -> None:
    # Arrange — the scheduled job must ACT, not just report: a timer that
    # only dry-runs would print a nightly report nobody reads while the
    # sprawl kept growing. The safety lives in the predicate, not in
    # withholding --apply.
    # Act
    job = _job("sac.worktree-gc")
    # Assert
    assert job.command == "sac worktree gc --apply --all"


def test_worktree_gc_command_sweeps_every_declared_repo() -> None:
    # Arrange — --all is only correct because it HAS a clean source (every
    # agent spec.workdir that is a local git repo toplevel). If that source
    # ever disappears, this command silently sweeps nothing.
    # Act
    job = _job("sac.worktree-gc")
    # Assert
    assert "--all" in job.command


def test_worktree_gc_cadence_is_daily() -> None:
    # Arrange — sprawl accumulates over days and the age gate is 24h, so a
    # faster pass could not remove anything a daily one would miss.
    # Act
    job = _job("sac.worktree-gc")
    # Assert
    assert job.on_unit_active_sec == "1d"


def test_fleet_reconcile_job_name_is_package_prefixed() -> None:
    # Arrange — the enforcer of "should be running => is running".
    # Act
    job = _job("sac.fleet-reconcile")
    # Assert
    assert job.name == "sac.fleet-reconcile"


def test_fleet_reconcile_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong
    # kind raises at construction and `ecosystem up` then silently DROPS
    # sac's whole provider (provider-isolated, WARN-only) — taking the OAuth
    # refresh, the drift check and the worktree GC down with it.
    # Act
    job = _job("sac.fleet-reconcile")
    # Assert
    assert job.kind == "timer"


def test_fleet_reconcile_command_is_the_applying_form() -> None:
    # Arrange — THIS JOB IS THE MECHANISM. `restart.policy` in ~93 specs is
    # dead code without it: `_lifecycle/_start.py` runs the loop that reads
    # it on a daemon thread inside the short-lived `sac agents start` CLI, so
    # the supervisor dies with the process that promised it. A scheduled
    # DRY-RUN would restore nothing — the whole point is `--apply`.
    # Act
    job = _job("sac.fleet-reconcile")
    # Assert
    assert job.command == "sac agents reconcile --apply"


def test_fleet_reconcile_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a dead agent stays dead. A no-op
    # pass is one batched `tmux list-sessions` plus a spec read each, so it
    # is cheap enough to run often.
    # Act
    job = _job("sac.fleet-reconcile")
    # Assert
    assert job.on_unit_active_sec == "5min"


def test_fleet_reconcile_timeout_outlives_a_capped_pass() -> None:
    # Arrange — the pathological pass restarts `--limit` agents, each a
    # stop+settle+start. A pass killed at this timeout is SAFE (the restart
    # history is persisted per restart, not at the end), but the timeout must
    # still comfortably exceed a normal pass or the enforcer never finishes.
    # Act
    job = _job("sac.fleet-reconcile")
    # Assert
    assert job.timeout_sec == 300


# ---------------------------------------------------------------------------
# sac.restart-login-expired-agents — the SIBLING enforcer. fleet-reconcile
# owns dead/no-session corpses; this timer owns the OTHER half fleet-reconcile
# explicitly leaves alone: a LIVE tmux session whose Claude cannot authenticate
# (a frozen "Login expired" banner), which only a restart clears. Named
# verb+object so the derived units read `sac.restart-login-expired-agents
# .timer` / `.service` — no "timer" in the name (systemd's suffix already
# conveys periodicity; embedding it would double to `.timer.timer`).
# ---------------------------------------------------------------------------


def test_restart_login_expired_job_name_is_package_prefixed() -> None:
    # Arrange — the auto-restarter for auth-dead-but-live agents.
    # Act
    job = _job("sac.restart-login-expired-agents")
    # Assert
    assert job.name == "sac.restart-login-expired-agents"


def test_restart_login_expired_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong kind
    # raises at construction and `ecosystem up` then silently DROPS sac's whole
    # provider (provider-isolated, WARN-only) — taking the OAuth refresh, the
    # drift check, the worktree GC AND the fleet-reconcile enforcer down too.
    # Act
    job = _job("sac.restart-login-expired-agents")
    # Assert
    assert job.kind == "timer"


def test_restart_login_expired_command_is_the_applying_form() -> None:
    # Arrange — a scheduled DRY-RUN would detect wedged agents and heal none.
    # The whole point is `--apply`. Detection stays read-only; the restart is
    # the only mutation.
    # Act
    job = _job("sac.restart-login-expired-agents")
    # Assert
    assert job.command == "sac agents restart-login-expired --apply"


def test_restart_login_expired_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a login-expired agent stays wedged,
    # matched to fleet-reconcile so the two enforcers sweep on the same beat.
    # Act
    job = _job("sac.restart-login-expired-agents")
    # Assert
    assert job.on_unit_active_sec == "5min"


def test_restart_login_expired_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field would drop the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("sac.restart-login-expired-agents")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)


# ---------------------------------------------------------------------------
# sac.sync-pr-cards — one board card per OPEN pull request, so the backlog is
# TRACKABLE. Named verb+object so the derived units read
# `sac.sync-pr-cards.timer` / `.service`.
#
# The gap it fills is narrow and was unowned: scitex-todo's stale-active sweep
# ALREADY nudges the owner of any untouched open card, but it can only nudge
# about cards that EXIST. An un-carded PR is invisible to it — which is how 35
# open PRs accumulated until 31 were force-closed BY HAND on 2026-07-18. sac
# supplies the FACT; scitex-todo supplies the reminder. Do not add nudge logic
# to sac: two nudgers with independent state is the double-supervisor class.
# ---------------------------------------------------------------------------


def test_sync_pr_cards_job_name_is_package_prefixed() -> None:
    # Arrange — the card-per-open-PR sweep.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.name == "sac.sync-pr-cards"


def test_sync_pr_cards_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong kind
    # raises at construction and `ecosystem up` then silently DROPS sac's whole
    # provider (provider-isolated, WARN-only) — taking the OAuth refresh, the
    # drift check, the worktree GC and BOTH fleet enforcers down with it.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.kind == "timer"


def test_sync_pr_cards_command_is_the_applying_form() -> None:
    # Arrange — a scheduled DRY-RUN would print a report nobody reads while the
    # backlog kept growing untracked (the same reasoning as the worktree GC).
    # The safety here is not withholding --apply: the destructive operation
    # (completing a card) is gated on a PROVEN-readable fetch instead.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.command.endswith("pr sync-cards --apply")


def test_sync_pr_cards_command_uses_an_absolute_sac_path() -> None:
    # Arrange — THE deploy hazard this pins. Five sac installs were measured on
    # this host on 2026-07-18 (0.21.24 / 0.21.22 / 0.21.21 / 0.21.11 / none),
    # and which one a bare `sac` resolves to depends on the invocation form.
    # systemd units get a minimal PATH that is NOT the operator's login PATH,
    # so a bare `sac` in a unit can silently execute months-old code while
    # reporting the same success. A bare name is not a stable reference.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.command.startswith("/")


def test_sac_bin_prefers_an_explicit_override(sac_bin_override) -> None:
    # Arrange — the override exists so a host that installs sac elsewhere is
    # not forced onto the hard-coded release path. Resolved PER CALL against
    # the REAL environment, so an env var set after import still wins (a module
    # constant would bake it — the trap _state.state_paths documents paying
    # for, where a fixture set $HOME and silently did nothing).
    from scitex_agent_container._jobs_plugin import sac_bin

    # Act
    resolved = sac_bin()
    # Assert
    assert resolved == sac_bin_override


def test_sac_bin_override_reaches_the_jobspec_command(sac_bin_override) -> None:
    # Arrange — the override must actually flow into the MATERIALISED unit, not
    # merely be resolvable in isolation. This is the leg that would catch
    # sac_bin() being called at import (and thus frozen) rather than per call.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.command == f"{sac_bin_override} pr sync-cards --apply"


def test_sync_pr_cards_cadence_is_thirty_minutes() -> None:
    # Arrange — the cadence IS the window a new PR stays untracked. It only has
    # to beat the shelf life (days) by a wide margin, and a no-op pass is one
    # `gh pr list` per repo, so 30min is generous while staying gentle on the
    # GitHub API.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.on_unit_active_sec == "30min"


def test_sync_pr_cards_timeout_outlives_a_large_backlog() -> None:
    # Arrange — one `gh pr list` per repo plus a card upsert per open PR. A
    # pass killed at this timeout is SAFE (each write is independent and
    # idempotent, and completion is gated on a readable fetch so a truncated
    # pass can never mass-complete cards), but it must still comfortably exceed
    # a normal pass.
    # Act
    job = _job("sac.sync-pr-cards")
    # Assert
    assert job.timeout_sec == 600


def test_provider_does_not_federate_a_pr_expiry_job() -> None:
    # Arrange — ABSENCE PIN, and it is deliberate. The 3-day PR shelf life is a
    # FLEET-WIDE rule (operator, 2026-07-18: 「3日ルールは全てのレポジトリで共通
    # です」) owned by scitex-dev as a shared primitive, under the standing rule
    # that dev holds primitives and leaves consume them.
    #
    # sac declaring its own expiry timer would FORK that primitive: two repos
    # hand-rolling "what does stale mean" makes the fleet's answer depend on
    # which tool you asked. An earlier draft of this branch DID declare it and
    # implement the logic; both were removed rather than left as a placeholder,
    # because a scheduled job calling a primitive that does not exist is a red
    # timer, and "we'll swap it later" is how this host ended up with five
    # differently-versioned sac installs.
    #
    # Unpin this ONLY when consuming dev's primitive — never to reintroduce a
    # sac-local staleness decision. See _prlifecycle/_expiry_seam.py.
    # Act
    names = [spec.name for spec in provide_jobs()]
    # Assert
    assert "sac.close-expired-prs" not in names
