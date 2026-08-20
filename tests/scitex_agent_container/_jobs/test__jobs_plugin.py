"""Tests for the ``scitex_dev.jobs`` provider (``_jobs_plugin``).

Verifies the JobSpecs sac registers under the ``scitex_dev.jobs``
entry-point group match the federated contract:

* ``scitex-agent-container-accounts-refresh`` — a periodic systemd timer job that runs
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

import re

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs._jobs_plugin import provide_jobs  # noqa: E402

from ._jobspec_helpers import _job, _split_command  # noqa: E402









def test_provider_jobs_are_real_jobspecs() -> None:
    # Arrange — call the registered provider.
    # Act
    jobs = provide_jobs()
    # Assert — every entry is the canonical contract type, not a look-alike.
    assert all(isinstance(job, jobs_mod.JobSpec) for job in jobs)


def test_provider_job_name_is_package_prefixed() -> None:
    # Arrange — call the registered provider.
    # Act
    job = _job("scitex-agent-container-accounts-refresh")
    # Assert
    assert job.name == "scitex-agent-container-accounts-refresh"


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
    job = _job("scitex-agent-container-accounts-refresh")
    # Assert
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 120", "accounts refresh --all --include-active --sync-active-login")


def test_provider_job_command_never_skips_active() -> None:
    # Arrange — a belt-and-braces guard: --skip-active must never
    # reappear in the sole-refresher timer, however the command is spelled.
    # Act
    job = _job("scitex-agent-container-accounts-refresh")
    # Assert
    assert "--skip-active" not in job.command


def test_provider_job_kind_is_timer() -> None:
    # Arrange — call the registered provider. The legacy ``kind=
    # "systemd"`` is no longer accepted by JobSpec.validate() since
    # scitex-dev #153; ``scitex-agent-container-accounts-refresh`` is a periodic
    # systemd --user timer (token TTL ~7h, refresh every 2h) so the
    # canonical kind is ``"timer"`` (lead msg c5212862, 2026-06-11).
    # Act
    job = _job("scitex-agent-container-accounts-refresh")
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
    job = _job("scitex-agent-container-accounts-refresh")
    # Assert
    assert job.on_unit_active_sec == "2h"


# ---------------------------------------------------------------------------
# sac.accounts-keepalive — the DISTRIBUTION half of the single-refresher
# model, and the SIBLING of scitex-agent-container-accounts-refresh above. That job rotates the
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






































# ---------------------------------------------------------------------------
# sac.restart-login-expired-agents — the SIBLING enforcer. fleet-reconcile
# owns dead/no-session corpses; this timer owns the OTHER half fleet-reconcile
# explicitly leaves alone: a LIVE tmux session whose Claude cannot authenticate
# (a frozen "Login expired" banner), which only a restart clears. Named
# verb+object so the derived units read `sac.restart-login-expired-agents
# .timer` / `.service` — no "timer" in the name (systemd's suffix already
# conveys periodicity; embedding it would double to `.timer.timer`).
# ---------------------------------------------------------------------------


















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


























# ---------------------------------------------------------------------------
# THE POPULATION INVARIANT — what lets these JobSpecs land on cron at all.
#
# The per-job pins above are pinned by name; these are pinned over EVERY job,
# because the way this contract breaks is by OMISSION in a job added later,
# which no name-based test would notice.
#
# The mechanism: `scitex-dev ecosystem up` lowers every kind="timer" JobSpec
# onto the managed crontab block (operator policy 2026-06-14), and a cron line
# is `<schedule> <command> # marker` and nothing else — there is no field a
# `timeout_sec` could ride in. scitex-dev's guard therefore REFUSES to lower a
# timer declaring one, and because `up` is all-or-nothing, ONE such job blocks
# every provider on the host. Measured 2026-08-17 on scitex-compute-04: nine
# sac jobs declared `timeout_sec`, `up` aborted, nothing could be armed.
#
# So the bound lives in the COMMAND, and the field that cannot travel is gone.
# ---------------------------------------------------------------------------

#: A self-bounding command: the coreutils binary, a whole-second bound, and a
#: non-empty payload after it.
_SELF_BOUNDING = re.compile(r"^/usr/bin/timeout (\d+) (\S.*)$")


def test_every_command_is_self_bounding() -> None:
    # Arrange — the bound must live in the COMMAND, the only part of a JobSpec
    # a cron line carries. Asserted over every job rather than an enumerated
    # set: a job added later without a bound is exactly the regression this
    # pins, and a list would not catch it.
    # Act
    unbounded = [
        job.name for job in provide_jobs() if not _SELF_BOUNDING.match(job.command)
    ]
    # Assert
    assert unbounded == []


def test_no_job_declares_timeout_sec() -> None:
    # Arrange — NOT a style rule. scitex-dev's lowering guard fires on
    # `timeout_sec is not None` and never inspects whether the command is
    # actually bounded, so a job carrying BOTH a literal `timeout` and this
    # field still refuses to lower and still aborts `ecosystem up` for the
    # whole host. The field is therefore dropped, not kept alongside.
    # Act
    declaring = [job.name for job in provide_jobs() if job.timeout_sec is not None]
    # Assert
    assert declaring == []


def test_no_command_is_double_bounded() -> None:
    # Arrange — the payload after the prefix must not itself be a `timeout`
    # invocation. Two nested bounds are not a stricter bound; they are a
    # second number nobody maintains, and the inner one silently wins.
    # Act
    doubled = [
        job.name
        for job in provide_jobs()
        if (m := _SELF_BOUNDING.match(job.command))
        and m.group(2).split()[0].endswith("timeout")
    ]
    # Assert
    assert doubled == []


def test_declared_bound_is_a_positive_number_of_seconds() -> None:
    # Arrange — `timeout 0 <cmd>` means "no timeout at all" in coreutils, so a
    # zero would read as a bound while removing one. Guard the value, not just
    # the shape.
    # Act
    bounds = {
        job.name: int(m.group(1))
        for job in provide_jobs()
        if (m := _SELF_BOUNDING.match(job.command))
    }
    # Assert
    assert all(seconds > 0 for seconds in bounds.values()), bounds


def test_the_prefix_wraps_something_in_every_job() -> None:
    # Arrange — a positive control on the regex above: prove it matches a real
    # payload in EVERY job, not merely in some. Anchoring on the job count (an
    # identity) rather than on `> 0` (a quantity) keeps this honest when the
    # provider list changes.
    # Act
    payloads = [
        m.group(2)
        for job in provide_jobs()
        if (m := _SELF_BOUNDING.match(job.command))
    ]
    # Assert
    assert len(payloads) == len(provide_jobs())


# ---------------------------------------------------------------------------
# The operator-facing outcome, measured through scitex-dev's OWN guard rather
# than restated in our words. Skips cleanly on a scitex-dev predating the
# lowering module, so CI never reddens on a version lag.
# ---------------------------------------------------------------------------


def _lowering():
    return pytest.importorskip(
        "scitex_dev._cli.ecosystem._cmds._up_timer_lowering",
        reason="installed scitex-dev predates the timer-lowering guard",
    )


def test_no_job_degrades_when_lowered_onto_cron() -> None:
    # Arrange — `degraded_job_names` is the exact predicate `ecosystem up`
    # reports under --allow-lossy-timer-lowering. Nine sac jobs were listed
    # there on 2026-08-17; the target is none.
    low = _lowering()
    # Act
    degraded = low.degraded_job_names(provide_jobs())
    # Assert
    assert degraded == []


def test_strict_lowering_does_not_abort_the_ecosystem() -> None:
    # Arrange — this is THE blocker being fixed. `ecosystem up` calls
    # `collect_cron_jobs` WITHOUT allow_lossy first; one raising job aborts the
    # run before anything is written, for every provider on the host.
    low = _lowering()
    jobs = provide_jobs()
    # Act — must not raise TimerLoweringError.
    _merged, _native, lowered = low.collect_cron_jobs(jobs, allow_lossy=False)
    # Assert
    assert lowered == len([job for job in jobs if job.kind == "timer"])


def test_strict_lowering_installs_every_declared_job() -> None:
    # Arrange — the companion to the abort check: proceeding is only the right
    # outcome if nothing was quietly dropped on the way through.
    low = _lowering()
    jobs = provide_jobs()
    # Act
    merged, _native, _lowered = low.collect_cron_jobs(jobs, allow_lossy=False)
    # Assert
    assert len(merged) == len(jobs)


def test_the_cron_line_that_lands_on_the_host_carries_the_bound() -> None:
    # Arrange — the end-to-end check, and the one that would have caught the
    # original defect: assert on the ARTIFACT (the crontab line) rather than on
    # the declaration. A `timeout_sec` assertion passed throughout the
    # 2026-07-18 incident while the deployed line was unbounded.
    low = _lowering()
    from scitex_dev.jobs import _cron_block as cron_block

    merged, _native, _lowered = low.collect_cron_jobs(provide_jobs(), allow_lossy=False)
    # Act
    lines = [cron_block.build_cron_line(spec) for spec in merged]
    # Assert
    assert all(" /usr/bin/timeout " in line for line in lines), lines


# The POPULATION guard over these jobs' payloads — the one that catches a
# JobSpec added tomorrow rather than any named above — lives in
# `test__specs_payload.py`. It needs the `executable` seam to resolve
# deterministically, which is a different setup from every test here.
