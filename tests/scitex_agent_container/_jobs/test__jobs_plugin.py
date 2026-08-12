"""Tests for the ``scitex_dev.jobs`` provider (``_jobs_plugin``).

Verifies the JobSpecs sac registers under the ``scitex_dev.jobs``
entry-point group match the federated contract:

* ``sac.accounts-refresh`` — a periodic systemd timer job that runs
  ``--all --include-active --sync-active-login`` every 2h (the SOLE
  refresher; see the ``--skip-active`` note below).
* ``sac.accounts-keepalive`` — the DISTRIBUTION half of that same
  single-refresher model. ``accounts-refresh`` rotates the token on the
  ONE host holding refresh material; this one copies the result out to
  the access-only hosts every 15min and proves each accepts it. The two
  are a pair, and the pair is why a fresh token on the master does not
  by itself keep the followers alive.
* ``sac.host-sync-check`` — the hourly READ-ONLY peer drift detector.
* ``sac.spartan-sif-bake`` — the every-10-minutes remote SIF bake on the Spartan
  lease + pull/verify/atomic-swap on the master (operator directive
  2026-07-17: bake on Spartan, rsync here, zero master CPU).
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

from scitex_agent_container._jobs._jobs_plugin import provide_jobs  # noqa: E402


def _job(name: str):
    (match,) = [j for j in provide_jobs() if j.name == name]
    return match


def test_provider_returns_every_expected_job() -> None:
    # Arrange — pinned by NAME, not by count. An exact-count assertion goes
    # red on every legitimate addition while saying nothing about WHICH job
    # moved, so the number gets bumped reflexively and the pin quietly stops
    # meaning anything. Membership names what actually changed.
    #
    # accounts-keepalive is the newest: the DISTRIBUTION half of the
    # single-refresher model, paired with accounts-refresh below. `sac
    # listen` is still NOT federated (see the module docstring and the
    # absence-pin below).
    # Act
    names = {job.name for job in provide_jobs()}
    # Assert
    assert names == {
        "sac.accounts-refresh",
        "scitex-agent-container-accounts-keepalive",
        "scitex-agent-container-host-sync-check",
        "scitex-agent-container-worktree-gc",
        "scitex-agent-container-spartan-sif-bake",
        "scitex-agent-container-fleet-reconcile",
        "scitex-agent-container-restart-login-expired-agents",
        "scitex-agent-container-heal-agent-auth",
        "scitex-agent-container-freshness-refresh",
    }


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


# ---------------------------------------------------------------------------
# sac.accounts-keepalive — the DISTRIBUTION half of the single-refresher
# model, and the SIBLING of sac.accounts-refresh above. That job rotates the
# token on the ONE host holding refresh material; this one copies the result
# out to the access-only hosts and proves each of them accepts it. Refreshing
# the master is not enough on its own: every other host holds a copy nothing
# on that box can renew, so without this job they 401 within one access-token
# lifetime (measured 2026-08-10, three fleet-wide deaths in a day).
#
# HOST PINNING IS NOT EXPRESSIBLE IN A JobSpec — there is no host field — so
# WHERE this runs is an operator install decision. The verb defends itself
# instead: `--all` resolves to the accounts THIS host holds refresh material
# for and exits non-zero when that set is empty, so an install on the wrong
# host is loud rather than quietly inert. Nothing here arms anything; a
# JobSpec is inert until `ecosystem up` installs it.
# ---------------------------------------------------------------------------


def test_accounts_keepalive_job_name_is_package_prefixed() -> None:
    # Arrange — the distribution half of the single-refresher model.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.name == "scitex-agent-container-accounts-keepalive"


def test_accounts_keepalive_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A kind
    # outside JobSpec.ALLOWED_KINDS raises at construction, and `ecosystem up`
    # then silently DROPS sac's WHOLE provider (provider-isolated, WARN-only)
    # — taking the OAuth refresh, the drift check, the worktree GC and both
    # heal enforcers down with it.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.kind == "timer"


def test_accounts_keepalive_command_pushes_to_every_access_only_peer() -> None:
    # Arrange — the peer list IS the job. A keepalive that reaches two of the
    # three access-only hosts looks healthy (exit 0, verified peers) while the
    # third silently expires, which is the exact failure this job exists to
    # end. Pin the whole command so dropping a `--to` is a red test, not a
    # host nobody notices is dead.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.command == (
        "sac accounts keepalive --all "
        "--to ywata-note-win "
        "--to scitex-compute-03 "
        "--to scitex-compute-04"
    )


def test_accounts_keepalive_command_runs_the_copying_verb_not_a_minting_one() -> None:
    # Arrange — belt-and-braces, the counterpart of accounts-refresh's
    # --skip-active guard. This job COPIES the master's current token; minting
    # rotates it, and a rotation revokes the token every running agent is
    # holding. Scheduling `accounts refresh` or `accounts login` here would
    # turn a keepalive into a fleet-wide logout every 15 minutes, so pin the
    # verb itself rather than trusting the full-command assertion above to be
    # re-read whenever someone edits the peer list.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.command.split()[:3] == ["sac", "accounts", "keepalive"]


def test_accounts_keepalive_cadence_bounds_the_follower_outage() -> None:
    # Arrange — the cadence IS the worst-case follower outage, not a guess at
    # when work is needed. The master's token changes once in ~7h at an
    # unpredictable moment, and the instant it does every follower's copy is
    # revoked; the tick only decides how long that revoked window lasts. A
    # converged peer is verified rather than rewritten, so the extra ticks are
    # near free — which is what makes 15min affordable.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.on_unit_active_sec == "15min"


def test_accounts_keepalive_schedule_mirrors_the_timer_cadence() -> None:
    # Arrange — the cron form is kept alongside the timer cadence (as every
    # sibling does) so `ecosystem cron` could derive an equivalent line, and
    # so the two spellings cannot silently disagree about how often this runs.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.schedule == "*/15 * * * *"


def test_accounts_keepalive_timeout_outlives_a_three_peer_pass() -> None:
    # Arrange — per peer: a handful of ssh ops plus ONE outbound HTTPS
    # verification (15s cap inside the probe). 300s covers three peers
    # including a slow one. A pass killed here is SAFE — nothing is published
    # unverified, so the peer keeps its previous credential.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.timeout_sec == 300


def test_accounts_keepalive_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field would drop the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)


def test_accounts_keepalive_and_accounts_refresh_are_both_declared() -> None:
    # Arrange — unlike the heal pair below, these two are NOT alternatives:
    # they are the two halves of one model and BOTH must be enabled. Refresh
    # without keepalive leaves the followers holding a revoked copy; keepalive
    # without refresh distributes a token nothing renews. Deleting either one
    # breaks the fleet in a way that looks like the other half working.
    # Act
    names = {job.name for job in provide_jobs()}
    # Assert
    assert {"sac.accounts-refresh", "scitex-agent-container-accounts-keepalive"} <= names


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
    # Act
    job = _job("scitex-agent-container-host-sync-check")
    # Assert — the command is precisely the read-only check+alarm form.
    assert job.command == "sac host sync --check --all --alarm"


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
    assert job.command == "sac worktree gc --apply --all"


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


def test_fleet_reconcile_job_name_is_package_prefixed() -> None:
    # Arrange — the enforcer of "should be running => is running".
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.name == "scitex-agent-container-fleet-reconcile"


def test_fleet_reconcile_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong
    # kind raises at construction and `ecosystem up` then silently DROPS
    # sac's whole provider (provider-isolated, WARN-only) — taking the OAuth
    # refresh, the drift check and the worktree GC down with it.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.kind == "timer"


def test_fleet_reconcile_command_is_the_applying_form() -> None:
    # Arrange — THIS JOB IS THE MECHANISM. `restart.policy` in ~93 specs is
    # dead code without it: `_lifecycle/_start.py` runs the loop that reads
    # it on a daemon thread inside the short-lived `sac agents start` CLI, so
    # the supervisor dies with the process that promised it. A scheduled
    # DRY-RUN would restore nothing — the whole point is `--apply`.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.command == "sac agents reconcile --apply"


def test_fleet_reconcile_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a dead agent stays dead. A no-op
    # pass is one batched `tmux list-sessions` plus a spec read each, so it
    # is cheap enough to run often.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.on_unit_active_sec == "5min"


def test_fleet_reconcile_timeout_outlives_a_capped_pass() -> None:
    # Arrange — the pathological pass restarts `--limit` agents, each a
    # stop+settle+start. A pass killed at this timeout is SAFE (the restart
    # history is persisted per restart, not at the end), but the timeout must
    # still comfortably exceed a normal pass or the enforcer never finishes.
    # Act
    job = _job("scitex-agent-container-fleet-reconcile")
    # Assert
    assert job.timeout_sec == 300


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
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.name == "scitex-agent-container-restart-login-expired-agents"


def test_restart_login_expired_job_kind_is_timer() -> None:
    # Arrange — a periodic systemd --user timer, so kind="timer". A wrong kind
    # raises at construction and `ecosystem up` then silently DROPS sac's whole
    # provider (provider-isolated, WARN-only) — taking the OAuth refresh, the
    # drift check, the worktree GC AND the fleet-reconcile enforcer down too.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
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
    assert job.command == "sac image bake-remote --yes"


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
    # slow link must fit; the per-leg ssh timeout is 7200s, so the unit
    # cap must exceed the worst legitimate chain or the timer kills its
    # own successful runs.
    # Act
    job = _job("scitex-agent-container-spartan-sif-bake")
    # Assert
    assert job.timeout_sec == 14_400


def test_restart_login_expired_command_is_the_applying_form() -> None:
    # Arrange — a scheduled DRY-RUN would detect wedged agents and heal none.
    # The whole point is `--apply`. Detection stays read-only; the restart is
    # the only mutation.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.command == "sac agents restart-login-expired --apply"


def test_restart_login_expired_cadence_is_five_minutes() -> None:
    # Arrange — the cadence IS the window a login-expired agent stays wedged,
    # matched to fleet-reconcile so the two enforcers sweep on the same beat.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert job.on_unit_active_sec == "5min"


def test_restart_login_expired_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field would drop the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("scitex-agent-container-restart-login-expired-agents")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)


# ---------------------------------------------------------------------------
# sac.heal-agent-auth — the INCUMBENT auth healer, migrated off the crontab.
#
# The migration is the point. `~/.dotfiles/src/.cron/copy_crontab` installs the
# tracked manifest WHOLESALE (`git show HEAD:.crontab_list | crontab -`), so a
# line absent from `.crontab_list` is erased on its next run — and auth-heal has
# no line in that manifest at all. That is why the wrapper exporting
# SAC_SECRETS_ENVRC kept reverting: a hand-added crontab line is temporary BY
# CONSTRUCTION. These tests pin the properties a crontab line could not give it
# (Persistent=true via kind="timer") and the ones a systemd --user unit would
# otherwise lose (an absolute, PATH-independent ExecStart).
#
# Named verb+object (`heal` + `agent-auth`) like its sibling above, so the
# derived units read `sac.heal-agent-auth.timer` / `.service` — no "timer" in
# the name, since systemd's suffix already conveys periodicity.
# ---------------------------------------------------------------------------


def test_heal_agent_auth_job_name_is_package_prefixed() -> None:
    # Arrange — the incumbent auth healer, now declared rather than hand-cronned.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.name == "scitex-agent-container-heal-agent-auth"


def test_heal_agent_auth_job_kind_is_timer() -> None:
    # Arrange — kind="timer" is what buys `Persistent=true` from scitex-dev's
    # renderer (a missed window fires on resume — the property the swept crontab
    # line never had). kind="cron" would materialise the very crontab line this
    # job exists to escape, and a kind outside {"service","timer","cron"} raises
    # at construction, silently dropping sac's WHOLE provider.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.kind == "timer"


def test_heal_agent_auth_cadence_preserves_the_incumbent_ten_minutes() -> None:
    # Arrange — 10min is the LIVE cadence, not a new choice: runtime/auth-heal.log
    # ticks 15:20 → 15:30 → 15:40 → 15:50 → 16:00, matching the `*/10` cron line.
    # Migrating a schedule is the wrong moment to also retune it.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.on_unit_active_sec == "10min"


def test_heal_agent_auth_schedule_mirrors_the_retired_cron_expression() -> None:
    # Arrange — the cron form is kept alongside the timer cadence (as every
    # sibling does) so the expression the crontab line used stays legible in the
    # spec, and so `ecosystem cron` could still derive an equivalent line.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.schedule == "*/10 * * * *"


def test_heal_agent_auth_interpreter_token_is_absolute() -> None:
    # Arrange — scitex-dev's `resolve_execstart` passes a head starting with "/"
    # through VERBATIM. An absolute head is therefore the only form that depends
    # on neither the ambient PATH nor which interpreter ran `ecosystem up`.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.command.split()[0].startswith("/")


def test_heal_agent_auth_script_token_is_absolute() -> None:
    # Arrange — a systemd --user unit gets a MINIMAL PATH and no meaningful cwd,
    # so the script argument must be absolute too; a relative path would exec
    # fine under cron's $HOME cwd and fail as status=127 under systemd.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.command.split()[1].startswith("/")


def test_heal_agent_auth_runs_the_venv_python_not_the_system_one() -> None:
    # Arrange — the script's own `#!/usr/bin/env python3` would resolve to the
    # SYSTEM python under systemd's minimal PATH, not the 3.11 venv the fleet
    # runs on. Naming the venv interpreter outright is what carries the "PATH
    # prefixed with .env-3.11/bin" requirement into a unit that has no PATH.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.command.startswith("/home/ywatanabe/.env-3.11/bin/python ")


def test_heal_agent_auth_targets_the_real_auth_heal_entrypoint() -> None:
    # Arrange — the entrypoint the cron line actually invokes, per the tracked
    # `~/.scitex/agent-container/bin/README.md` ("run from cron; cron lines live
    # in ~/.dotfiles/.crontab_list") and corroborated by the live state/log files
    # that script writes. It takes no arguments — its `main()` has no argparse,
    # only a `--selftest` branch — so the bare script path is the whole command.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.command.endswith(
        "/home/ywatanabe/.scitex/agent-container/bin/auth-heal.py"
    )


def test_heal_agent_auth_timeout_outlives_a_restarting_pass() -> None:
    # Arrange — a no-op tick takes ~2-8s; the worst observed pass (a TUI restart
    # at 15:30:04 settling by 15:32:08) ~2min. A pass killed here is SAFE (state
    # is persisted per restart), but the timeout must still outlive a real one.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert job.timeout_sec == 300


def test_heal_agent_auth_constructs_as_a_real_jobspec() -> None:
    # Arrange — construction must not raise (a bad field would drop the whole
    # provider). Assert it is the canonical contract type, not a look-alike.
    # Act
    job = _job("scitex-agent-container-heal-agent-auth")
    # Assert
    assert isinstance(job, jobs_mod.JobSpec)


def test_heal_agent_auth_and_restart_login_expired_are_both_declared() -> None:
    # Arrange — declaring both is SAFE and deliberate: a JobSpec is inert until
    # `ecosystem up` installs it, so version-controlling the incumbent does not
    # enable it. What must never happen is both being ENABLED — they are the two
    # implementations of the same TUI heal, with INDEPENDENT debounce state, and
    # two restarters on one fleet is the documented double-supervisor hazard.
    # This pins that the choice stays an operator deploy decision rather than
    # being silently foreclosed by deleting one of them.
    # Act
    names = {job.name for job in provide_jobs()}
    # Assert
    assert {"scitex-agent-container-heal-agent-auth", "scitex-agent-container-restart-login-expired-agents"} <= names
