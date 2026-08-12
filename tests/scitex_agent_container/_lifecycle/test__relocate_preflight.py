"""Rewriting `host:` alone yields an agent that starts, reports healthy, and does nothing.

That is what happened on 2026-08-07, and it is the worst failure shape available
because it looks exactly like success. Every check in `_relocate_preflight`
exists because one specific thing went wrong that day, so the tests are written
against those exact broken states — a check nobody has seen fail is a check
nobody knows would have caught it.

The property doing most of the work is the one about UNKNOWN. A probe that could
not answer must not read as a pass; the 08-07 move reported healthy partly
because an unanswered credential question was treated as fine. So the aggregate
verdict is three-valued and refuses on unknowns as firmly as on failures, while
still reporting the two separately.

Pure predicates over observed facts. No I/O, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_origin import RepoWork
from scitex_agent_container._lifecycle._relocate_preflight import (
    CHECK_BINDS,
    CHECK_CARD_STORE,
    CHECK_CARD_STORE_DSN,
    CHECK_CREDENTIALS,
    CHECK_GROUPS,
    CHECK_HUB_FROM_TARGET,
    CHECK_IMAGE,
    CHECK_PORTS,
    CHECK_REACHABLE,
    CHECK_RUNTIME,
    CHECK_SAC_PRESENT,
    CHECK_SCHEMA,
    CHECK_SESSION,
    CHECK_SOURCE_WORK,
    CHECK_WORKDIR,
    Check,
    SourceFacts,
    TargetFacts,
    preflight,
)

AGENT = "scitex-agent-container"
DST = "nas-03"
SRC = "ywata-note-win"
RUNTIME = "apptainer"
PORTS = (19019,)


def _healthy_facts(**overrides: object) -> TargetFacts:
    """A target where every fact was observed and every one is good."""
    base = dict(
        reachable=True,
        image_present=True,
        missing_bind_sources=(),
        missing_workdir_paths=(),
        target_resolved_groups=("developer", "infra"),
        card_store_url="postgresql://scitex_cards@127.0.0.1:55432/scitex_cards",
        card_store_reachable=True,
        credential_expires_in_s=3600.0,
        credential_refresh_token_present=True,
        supported_runtimes=("apptainer", "tui"),
        rejected_spec_keys=(),
        ports_in_use=(),
        hub_reachable_from_target=True,
        sac_on_path=True,
        sac_resolved_path="/usr/local/bin/sac",
    )
    base.update(overrides)
    return TargetFacts(**base)  # type: ignore[arg-type]


def _clean_source(**overrides: object) -> SourceFacts:
    """A source that was scanned, holds nothing un-saved, and names its session.

    The transcripts and the marker are part of "fully observed" because the
    session check reads them. Several transcripts is the ORDINARY shape — every
    one of the ten agents measured on 2026-08-12 held between two and five — so
    the healthy fixture holds three rather than the one the old guard required.
    """
    base = dict(
        repos=(RepoWork(path="/repo", branch="develop", uncommitted=0, unpushed=0),),
        transcripts=(("aaa1.jsonl", 1000), ("bbb2.jsonl", 3000), ("ccc3.jsonl", 2000)),
        session_marker="aaa1",
    )
    base.update(overrides)
    return SourceFacts(**base)  # type: ignore[arg-type]


WORKDIR = "/home/ywatanabe/proj/scitex-agent-container"
GROUPS = ("developer", "infra")


def _run(source_facts: SourceFacts | None = None, **overrides: object):
    return preflight(
        agent=AGENT,
        to_host=DST,
        facts=_healthy_facts(**overrides),
        runtime=RUNTIME,
        required_ports=PORTS,
        source_facts=source_facts if source_facts is not None else _clean_source(),
        from_host=SRC,
        workdir=WORKDIR,
        declared_groups=GROUPS,
    )


def _named(report, name: str) -> Check:
    return next(c for c in report.checks if c.name == name)


# ---------------------------------------------------------------------------
# the aggregate verdict — unknown is not a pass
# ---------------------------------------------------------------------------


def test_a_fully_observed_healthy_target_passes() -> None:
    # Arrange
    report = _run()
    # Act
    verdict = report.ok
    # Assert
    assert verdict is True


def test_any_failure_blocks_the_relocation() -> None:
    # Arrange
    report = _run(image_present=False)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is False


def test_an_unobserved_fact_does_not_read_as_a_pass() -> None:
    # Arrange: the 08-07 move reported healthy partly because an unanswered
    # question was treated as fine.
    report = _run(credential_expires_in_s=None, credential_refresh_token_present=None)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is None


def test_an_entirely_unobserved_target_is_unknown_not_ok() -> None:
    # Arrange: a prober that ran nothing must not produce a go.
    report = preflight(agent=AGENT, to_host=DST, facts=TargetFacts(), runtime=RUNTIME)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is None


# ---------------------------------------------------------------------------
# session_resolvable — asked HERE because the phase that needs it runs late
# ---------------------------------------------------------------------------


def test_several_transcripts_with_a_marker_pass_the_session_check() -> None:
    # Arrange: THE shape every remaining agent has. Three transcripts is normal,
    # and the old `len(files) == 1` guard made it fatal.
    report = _run()
    # Act
    check = _named(report, CHECK_SESSION)
    # Assert
    assert check.ok is True


def test_an_agent_whose_session_cannot_be_resolved_fails_preflight() -> None:
    # Arrange: THE gap. Ten agents returned "GO — every check passed" on
    # 2026-08-12 and then aborted at TARGET_STANDBY with the agent stopped, the
    # transcript copied and no marker written. The refusal has to happen while
    # the agent is still up.
    report = _run(source_facts=_clean_source(session_marker="not-carried"))
    # Act
    verdict = report.ok
    # Assert
    assert verdict is False


def test_that_failure_is_reported_under_the_session_check() -> None:
    # Arrange: a blocked relocation is only actionable if the report names WHICH
    # question blocked it.
    report = _run(source_facts=_clean_source(session_marker="not-carried"))
    # Act
    check = _named(report, CHECK_SESSION)
    # Assert
    assert check.ok is False


def test_that_failure_names_the_transcripts_it_saw() -> None:
    # Arrange: the operator resolves this by seeding the marker, which needs the
    # candidate list in front of them.
    report = _run(source_facts=_clean_source(session_marker="not-carried"))
    # Act
    check = _named(report, CHECK_SESSION)
    # Assert
    assert "bbb2.jsonl" in check.detail


def test_a_source_whose_transcripts_were_never_listed_is_unknown() -> None:
    # Arrange: nobody looked. That must not read as "there is one and it is
    # fine", which is how this class of bug reaches the phase that stops things.
    report = _run(source_facts=_clean_source(transcripts=None))
    # Act
    check = _named(report, CHECK_SESSION)
    # Assert
    assert check.ok is None


def test_an_agent_with_no_transcript_at_all_fails_the_session_check() -> None:
    # Arrange: an observed empty directory. The agent would boot on the target
    # with no memory — the 2026-08-07 outcome, and it must be said out loud
    # rather than discovered afterwards.
    report = _run(source_facts=_clean_source(transcripts=(), session_marker=""))
    # Act
    check = _named(report, CHECK_SESSION)
    # Assert
    assert check.ok is False


def test_every_check_is_unknown_when_nothing_was_observed() -> None:
    # Arrange: declared_groups is supplied so the groups check has something to
    # be unknown ABOUT — a spec claiming no groups passes it by construction.
    report = preflight(
        agent=AGENT,
        to_host=DST,
        facts=TargetFacts(),
        runtime=RUNTIME,
        declared_groups=GROUPS,
    )
    # Act
    unknown = report.unknown
    # Assert
    assert len(unknown) == len(report.checks)


def test_failures_are_reported_apart_from_unknowns() -> None:
    # Arrange: "this is wrong" and "I could not tell" call for different actions.
    report = _run(image_present=False, hub_reachable_from_target=None)
    # Act
    counts = (len(report.failed), len(report.unknown))
    # Assert
    assert counts == (1, 1)


def test_a_failure_outranks_an_unknown_in_the_verdict() -> None:
    # Arrange
    report = _run(image_present=False, hub_reachable_from_target=None)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is False


def test_a_clean_target_has_no_blocking_reasons() -> None:
    # Arrange
    report = _run()
    # Act
    reasons = report.blocking_reasons()
    # Assert
    assert reasons == ()


def test_every_blocking_reason_carries_its_hint() -> None:
    # Arrange: an error that only states what broke is half-written.
    report = _run(image_present=False)
    # Act
    reason = report.blocking_reasons()[0]
    # Assert
    assert "->" in reason


def test_the_report_runs_every_check_rather_than_stopping_at_the_first() -> None:
    # Arrange: a dry run that stops at the first problem makes the operator run
    # it N times to find N problems.
    report = _run(image_present=False, reachable=False, ports_in_use=(19019,))
    # Act
    failed = len(report.failed)
    # Assert
    assert failed == 3


# ---------------------------------------------------------------------------
# credentials — VALIDITY, not presence
# ---------------------------------------------------------------------------


def test_an_expired_target_credential_fails() -> None:
    # Arrange: the nas had one expired since 2026-05-23, and sac loaded it IN
    # PREFERENCE to the good one. Presence would have passed.
    report = _run(credential_expires_in_s=-86400.0)
    # Act
    check = _named(report, CHECK_CREDENTIALS)
    # Assert
    assert check.ok is False


def test_an_expired_credential_says_health_will_lie() -> None:
    # Arrange: the reason this is dangerous is that the agent looks fine.
    report = _run(credential_expires_in_s=-1.0)
    # Act
    check = _named(report, CHECK_CREDENTIALS)
    # Assert
    assert "401" in check.hint


def test_a_credential_with_no_refresh_token_fails() -> None:
    # Arrange: valid now, unrenewable — it dies at the first refresh.
    report = _run(credential_refresh_token_present=False)
    # Act
    check = _named(report, CHECK_CREDENTIALS)
    # Assert
    assert check.ok is False


def test_a_valid_renewable_credential_passes() -> None:
    # Arrange
    report = _run()
    # Act
    check = _named(report, CHECK_CREDENTIALS)
    # Assert
    assert check.ok is True


# ---------------------------------------------------------------------------
# the rest of the 2026-08-07 list
# ---------------------------------------------------------------------------


def test_a_bind_source_absent_on_the_target_fails() -> None:
    # Arrange: /mnt/c is a Windows drive that does not exist on the nas.
    report = _run(missing_bind_sources=("/mnt/c",))
    # Act
    check = _named(report, CHECK_BINDS)
    # Assert
    assert check.ok is False


def test_the_failing_bind_is_named() -> None:
    # Arrange: naming the offending value is what makes the hint actionable.
    report = _run(missing_bind_sources=("/mnt/c",))
    # Act
    check = _named(report, CHECK_BINDS)
    # Assert
    assert "/mnt/c" in check.detail


def test_an_unreachable_card_store_fails() -> None:
    # Arrange: 5432 here, 5442 there — an agent that cannot reach its board
    # runs and records nothing.
    report = _run(card_store_reachable=False)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is False


def test_a_runtime_the_target_rejects_fails() -> None:
    # Arrange: the nas's sac 0.21.9 rejected 'tui'.
    report = preflight(
        agent=AGENT,
        to_host=DST,
        facts=_healthy_facts(supported_runtimes=("apptainer",)),
        runtime="tui",
        required_ports=PORTS,
    )
    # Act
    check = _named(report, CHECK_RUNTIME)
    # Assert
    assert check.ok is False


def test_a_spec_key_the_target_rejects_fails() -> None:
    # Arrange: a top-level 'provider:' key, rejected by the older validator.
    report = _run(rejected_spec_keys=("provider",))
    # Act
    check = _named(report, CHECK_SCHEMA)
    # Assert
    assert check.ok is False


def test_a_port_already_in_use_on_the_target_fails() -> None:
    # Arrange
    report = _run(ports_in_use=(19019,))
    # Act
    check = _named(report, CHECK_PORTS)
    # Assert
    assert check.ok is False


def test_an_unrelated_busy_port_does_not_fail() -> None:
    # Arrange: only the ports this agent needs matter.
    report = _run(ports_in_use=(22, 5432))
    # Act
    check = _named(report, CHECK_PORTS)
    # Assert
    assert check.ok is True


def test_a_hub_unreachable_from_the_target_fails() -> None:
    # Arrange: reaching it from HERE proves nothing about THERE — the nas binds
    # 127.0.0.1, so nothing cross-host reaches it.
    report = _run(hub_reachable_from_target=False)
    # Act
    check = _named(report, CHECK_HUB_FROM_TARGET)
    # Assert
    assert check.ok is False


def test_the_hub_hint_explains_why_local_success_proves_nothing() -> None:
    # Arrange
    report = _run(hub_reachable_from_target=False)
    # Act
    check = _named(report, CHECK_HUB_FROM_TARGET)
    # Assert
    assert "127.0.0.1" in check.hint


# ---------------------------------------------------------------------------
# the shapes validate themselves
# ---------------------------------------------------------------------------


def test_a_non_passing_check_must_carry_a_hint() -> None:
    # Arrange: an error that only says what broke is half-written.
    fields = dict(name="x", ok=False, detail="broken", hint="")

    # Act
    def build() -> Check:
        return Check(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_an_unknown_check_must_also_carry_a_hint() -> None:
    # Arrange: "I could not tell" is only useful with "here is how to tell".
    fields = dict(name="x", ok=None, detail="unobserved", hint="")

    # Act
    def build() -> Check:
        return Check(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_check_refuses_an_empty_detail() -> None:
    # Arrange
    fields = dict(name="x", ok=True, detail="")

    # Act
    def build() -> Check:
        return Check(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_an_unobserved_check_still_explains_what_to_run() -> None:
    # Arrange
    report = preflight(agent=AGENT, to_host=DST, facts=TargetFacts(), runtime=RUNTIME)
    # Act
    check = report.unknown[0]
    # Assert
    assert "probe" in check.hint


# ---------------------------------------------------------------------------
# sac_present_on_target — installed and findable are two questions
#
# Measured 2026-08-11 on scitex-compute-04: sac lives at
# /home/ywatanabe/.env-sac/bin/sac and is absent from the non-interactive ssh
# PATH, so `ssh compute-04 sac …` answers "No such file or directory" — the same
# words a machine with no sac at all produces, needing the opposite fix.
# ---------------------------------------------------------------------------


def test_sac_on_the_ssh_path_passes() -> None:
    # Arrange
    report = _run()
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert check.ok is True


def test_sac_installed_but_off_the_ssh_path_fails() -> None:
    # Arrange: the compute-04 case.
    report = _run(
        sac_on_path=False, sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac"
    )
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert check.ok is False


def test_sac_off_the_path_is_reported_as_installed_rather_than_missing() -> None:
    # Arrange
    report = _run(
        sac_on_path=False, sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac"
    )
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert "IS INSTALLED" in check.detail


def test_sac_off_the_path_names_where_it_actually_is() -> None:
    # Arrange
    report = _run(
        sac_on_path=False, sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac"
    )
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert "/home/ywatanabe/.env-sac/bin/sac" in check.detail


def test_sac_off_the_path_tells_the_reader_not_to_install_a_second_copy() -> None:
    # Arrange: the wrong fix here is to install sac again, which is what a
    # single "sac not found" message would send the reader off to do.
    report = _run(
        sac_on_path=False, sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac"
    )
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert "do NOT install a second copy" in check.hint


def test_sac_absent_everywhere_fails_as_not_installed() -> None:
    # Arrange: "" means looked and found nothing.
    report = _run(sac_on_path=False, sac_resolved_path="")
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert "NOT INSTALLED" in check.detail


def test_sac_absent_everywhere_asks_for_an_install() -> None:
    # Arrange
    report = _run(sac_on_path=False, sac_resolved_path="")
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert "install sac" in check.hint


def test_sac_off_the_path_with_no_direct_lookup_is_unknown() -> None:
    # Arrange: not-on-PATH alone cannot tell the two failures apart, and
    # guessing either way sends the reader to the wrong fix.
    report = _run(sac_on_path=False, sac_resolved_path=None)
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert check.ok is None


def test_an_unprobed_sac_presence_is_unknown() -> None:
    # Arrange
    report = _run(sac_on_path=None, sac_resolved_path=None)
    # Act
    check = _named(report, CHECK_SAC_PRESENT)
    # Assert
    assert check.ok is None


# ---------------------------------------------------------------------------
# source_work_committed — relocating away from unsaved work strands it
# ---------------------------------------------------------------------------


def test_a_scanned_and_clean_source_passes() -> None:
    # Arrange
    report = _run()
    # Act
    check = _named(report, CHECK_SOURCE_WORK)
    # Assert
    assert check.ok is True


def test_uncommitted_work_on_the_source_fails_the_relocation() -> None:
    # Arrange
    source = SourceFacts(
        repos=(RepoWork(path="/proj/sac", branch="feat/x", uncommitted=7, unpushed=0),)
    )
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert check.ok is False


def test_the_uncommitted_failure_reports_the_file_count() -> None:
    # Arrange
    source = SourceFacts(
        repos=(RepoWork(path="/proj/sac", branch="feat/x", uncommitted=7, unpushed=0),)
    )
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert "7 uncommitted file(s)" in check.detail


def test_the_uncommitted_failure_reports_the_repo_path() -> None:
    # Arrange
    source = SourceFacts(
        repos=(RepoWork(path="/proj/sac", branch="feat/x", uncommitted=7, unpushed=0),)
    )
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert "/proj/sac" in check.detail


def test_unpushed_commits_on_the_source_fail_too() -> None:
    # Arrange: a branch pushed nowhere is unreachable from any other machine.
    source = SourceFacts(
        repos=(RepoWork(path="/proj/sac", branch="feat/x", uncommitted=0, unpushed=3),)
    )
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert "3 unpushed commit(s)" in check.detail


def test_an_unscanned_repo_is_unknown_rather_than_clean() -> None:
    # Arrange: a failed `git status` prints nothing, exactly like a clean tree.
    source = SourceFacts(repos=(RepoWork(path="/proj/sac"),))
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert check.ok is None


def test_a_source_nobody_looked_at_refuses_the_relocation() -> None:
    # Arrange: the default. A caller that has not looked at the source has not
    # established the move is safe.
    report = preflight(
        agent=AGENT, to_host=DST, facts=_healthy_facts(), runtime=RUNTIME
    )
    # Act
    check = _named(report, CHECK_SOURCE_WORK)
    # Assert
    assert check.ok is None


def test_a_source_with_no_repos_at_all_passes_when_it_was_scanned() -> None:
    # Arrange: an observed "nothing to strand" is different from "nobody asked".
    source = SourceFacts(repos=())
    # Act
    check = _named(_run(source_facts=source), CHECK_SOURCE_WORK)
    # Assert
    assert check.ok is True


# ---------------------------------------------------------------------------
# ONE CASE PER CHECK WHERE THE PROBE ITSELF FAILED
#
# The operator's requirement, 2026-08-11, and the bug this fleet ships most
# often: a probe that could not answer must report UNKNOWN, must not read as a
# pass, and must not be dressed up as a definite failure. Both mistakes have a
# cost and they are different costs — treating unreachable-as-fine relocates into
# a machine nobody verified; treating it as a fail sends somebody to debug a spec
# that is perfectly correct.
#
# Parametrized over EVERY check so a new one cannot be added without an answer
# here: the facts that feed it are blanked (which is exactly what
# `_relocate_probe.probe` does when a prober raises) and the verdict is asserted.
# ---------------------------------------------------------------------------

#: check name -> the observations to blank so THAT check, and only that check,
#: loses its evidence. Source-side checks are blanked on SourceFacts instead.
_BLANKING: dict[str, dict[str, object]] = {
    CHECK_REACHABLE: {"reachable": None},
    CHECK_IMAGE: {"image_present": None},
    CHECK_BINDS: {"missing_bind_sources": None},
    CHECK_WORKDIR: {"missing_workdir_paths": None},
    CHECK_CARD_STORE_DSN: {"card_store_url": None},
    CHECK_CARD_STORE: {"card_store_reachable": None},
    CHECK_CREDENTIALS: {
        "credential_expires_in_s": None,
        "credential_refresh_token_present": None,
    },
    CHECK_RUNTIME: {"supported_runtimes": None},
    CHECK_SCHEMA: {"rejected_spec_keys": None},
    CHECK_PORTS: {"ports_in_use": None},
    CHECK_GROUPS: {"target_resolved_groups": None},
    CHECK_HUB_FROM_TARGET: {"hub_reachable_from_target": None},
    CHECK_SAC_PRESENT: {"sac_on_path": None, "sac_resolved_path": None},
}

_SOURCE_BLANKING: dict[str, dict[str, object]] = {
    CHECK_SOURCE_WORK: {"repos": None},
    CHECK_SESSION: {"transcripts": None},
}


def _with_failed_probe(check_name: str):
    if check_name in _SOURCE_BLANKING:
        return _run(source_facts=_clean_source(**_SOURCE_BLANKING[check_name]))
    return _run(**_BLANKING[check_name])


@pytest.mark.parametrize("check_name", sorted(_BLANKING) + sorted(_SOURCE_BLANKING))
def test_a_probe_that_failed_makes_its_check_unknown(check_name: str) -> None:
    # Arrange: the prober raised, so the fact is None — never False.
    report = _with_failed_probe(check_name)
    # Act
    check = _named(report, check_name)
    # Assert
    assert check.ok is None


@pytest.mark.parametrize("check_name", sorted(_BLANKING) + sorted(_SOURCE_BLANKING))
def test_a_probe_that_failed_is_not_reported_as_a_failure(check_name: str) -> None:
    # Arrange: an unknown dressed as a fail sends somebody to fix a correct spec.
    report = _with_failed_probe(check_name)
    # Act
    failed = [c.name for c in report.failed]
    # Assert
    assert check_name not in failed


@pytest.mark.parametrize("check_name", sorted(_BLANKING) + sorted(_SOURCE_BLANKING))
def test_a_probe_that_failed_blocks_the_relocation(check_name: str) -> None:
    # Arrange: unknown refuses as firmly as fail. That is RELOCATION's policy,
    # applied at the aggregation site, not baked into each check.
    report = _with_failed_probe(check_name)
    # Act
    blocks = report.blocks
    # Assert
    assert blocks is True


@pytest.mark.parametrize("check_name", sorted(_BLANKING) + sorted(_SOURCE_BLANKING))
def test_a_probe_that_failed_keeps_the_verdict_out_of_false(check_name: str) -> None:
    # Arrange: the whole report must say "could not tell", not "no".
    report = _with_failed_probe(check_name)
    # Act
    verdict = report.ok
    # Assert
    assert verdict is None


@pytest.mark.parametrize("check_name", sorted(_BLANKING) + sorted(_SOURCE_BLANKING))
def test_every_unknown_check_says_how_to_answer_it(check_name: str) -> None:
    # Arrange: "I could not tell" is only useful with "here is how to tell".
    report = _with_failed_probe(check_name)
    # Act
    check = _named(report, check_name)
    # Assert
    assert len(check.hint) > 20


def test_the_blanking_table_covers_every_check_there_is() -> None:
    # Arrange: a new check added without an unknown-case answer here is exactly
    # the omission that lets an unmeasured question read as a pass.
    report = _run()
    covered = set(_BLANKING) | set(_SOURCE_BLANKING)
    # Act
    uncovered = {c.name for c in report.checks} - covered
    # Assert
    assert uncovered == set()


# ---------------------------------------------------------------------------
# the three checks added 2026-08-11: does the SPEC hold on the TARGET
# ---------------------------------------------------------------------------


def test_a_workdir_absent_on_the_target_fails() -> None:
    # Arrange: spec.workdir becomes apptainer's --pwd. Absent, the agent fails at
    # boot — which under relocation is after the source has been stopped.
    report = _run(missing_workdir_paths=("/home/ywatanabe/proj/scitex-hpc",))
    # Act
    check = _named(report, CHECK_WORKDIR)
    # Assert
    assert check.ok is False


def test_the_missing_workdir_is_named_with_its_host() -> None:
    # Arrange: a path without a vantage point is a fix applied on the wrong machine.
    report = _run(missing_workdir_paths=("/home/ywatanabe/proj/scitex-hpc",))
    # Act
    check = _named(report, CHECK_WORKDIR)
    # Assert
    assert "/home/ywatanabe/proj/scitex-hpc" in check.detail and DST in check.detail


def test_a_card_store_dsn_naming_5432_fails_however_reachable_it_is() -> None:
    # Arrange: THE point of this check. Something answers on 5432 nearly
    # everywhere, so reachability passes while the agent writes its cards into a
    # database nobody reads.
    report = _run(
        card_store_url="postgresql://scitex_cards@127.0.0.1:5432/scitex_cards",
        card_store_reachable=True,
    )
    # Act
    check = _named(report, CHECK_CARD_STORE_DSN)
    # Assert
    assert check.ok is False


def test_a_port_less_dsn_fails_because_libpq_defaults_it_to_5432() -> None:
    # Arrange: the same wrong endpoint, wearing no number at all.
    report = _run(card_store_url="postgresql://scitex_cards@127.0.0.1/scitex_cards")
    # Act
    check = _named(report, CHECK_CARD_STORE_DSN)
    # Assert
    assert check.ok is False


def test_the_dsn_hint_names_the_fleet_port() -> None:
    # Arrange
    report = _run(card_store_url="postgresql://scitex_cards@127.0.0.1:5432/scitex_cards")
    # Act
    check = _named(report, CHECK_CARD_STORE_DSN)
    # Assert
    assert "55432" in check.hint


def test_the_fleet_dsn_passes() -> None:
    # Arrange
    report = _run()
    # Act
    check = _named(report, CHECK_CARD_STORE_DSN)
    # Assert
    assert check.ok is True


def test_a_target_that_resolves_none_of_the_declared_groups_is_unknown() -> None:
    # Arrange: THE 2026-08-11 shape — three hosts answer [] for every agent
    # regardless of spec.yaml, and nine probes were refused 403 by it. Reporting
    # that as a FAIL sends the operator to edit a correct spec.
    report = _run(target_resolved_groups=())
    # Act
    check = _named(report, CHECK_GROUPS)
    # Assert
    assert check.ok is None


def test_a_target_that_resolves_some_but_not_all_groups_fails() -> None:
    # Arrange: the target ANSWERED, and the answer is no. That is a verdict.
    report = _run(target_resolved_groups=("developer",))
    # Act
    check = _named(report, CHECK_GROUPS)
    # Assert
    assert check.ok is False


def test_that_group_failure_names_the_group_the_target_does_not_know() -> None:
    # Arrange
    report = _run(target_resolved_groups=("developer",))
    # Act
    check = _named(report, CHECK_GROUPS)
    # Assert
    assert "infra" in check.detail


def test_a_spec_declaring_no_groups_passes_the_group_check() -> None:
    # Arrange: nothing claimed, nothing to hold. Observed by construction.
    report = preflight(
        agent=AGENT,
        to_host=DST,
        facts=_healthy_facts(target_resolved_groups=()),
        runtime=RUNTIME,
        source_facts=_clean_source(),
        declared_groups=(),
    )
    # Act
    check = _named(report, CHECK_GROUPS)
    # Assert
    assert check.ok is True
