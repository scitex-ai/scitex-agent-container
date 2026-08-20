"""The accounts-keepalive JobSpec — the DISTRIBUTION half of the token model.

Split out of ``test__jobs_plugin.py`` (at the per-file cap), mirroring the
split of :mod:`scitex_agent_container._jobs._specs_accounts` on the source
side, so the tests for a group sit beside the module that defines it.

``accounts-refresh`` rotates the token on the ONE host holding refresh
material; this job copies the result out to the access-only hosts and proves
each accepts it. The two are a pair, and the pair is why a fresh token on the
master does not by itself keep the followers alive — which is why these
assertions pin the peer list and the copying verb, not just the cadence.
"""

from __future__ import annotations


import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs._jobs_plugin import provide_jobs  # noqa: E402

from ._jobspec_helpers import _job, _split_command  # noqa: E402


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
    bound, _payload, rest = _split_command(job.command)
    assert (bound, rest) == ("/usr/bin/timeout 300", "accounts keepalive --all --to ywata-note-win --to scitex-compute-03 --to scitex-compute-04")

def test_accounts_keepalive_command_runs_the_copying_verb_not_a_minting_one() -> None:
    # Arrange — belt-and-braces, the counterpart of accounts-refresh's
    # --skip-active guard. This job COPIES the master's current token; minting
    # rotates it, and a rotation revokes the token every running agent is
    # holding. Scheduling `accounts refresh` or `accounts login` here would
    # turn a keepalive into a fleet-wide logout every 15 minutes, so pin the
    # verb itself rather than trusting the full-command assertion above to be
    # re-read whenever someone edits the peer list.
    #
    # Indexed PAST the self-bounding `/usr/bin/timeout <N>` prefix (tokens 0
    # and 1), so this still pins the VERB rather than the wrapper.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    _bound, _payload, rest = _split_command(job.command)
    assert rest.split()[:2] == ["accounts", "keepalive"]

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
    #
    # The bound is asserted on the COMMAND, not on `timeout_sec`, because
    # this job is deployed to cron and a cron line cannot carry that field.
    # Act
    job = _job("scitex-agent-container-accounts-keepalive")
    # Assert
    assert job.command.startswith("/usr/bin/timeout 300 ")

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
    assert {"scitex-agent-container-accounts-refresh", "scitex-agent-container-accounts-keepalive"} <= names
