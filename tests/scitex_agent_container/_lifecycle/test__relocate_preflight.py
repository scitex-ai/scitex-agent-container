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
    CHECK_CREDENTIALS,
    CHECK_HUB_FROM_TARGET,
    CHECK_PORTS,
    CHECK_RUNTIME,
    CHECK_SAC_PRESENT,
    CHECK_SCHEMA,
    CHECK_SESSION,
    CHECK_SOURCE_WORK,
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
        card_store_url="postgresql://scitex_cards@127.0.0.1:5442/scitex_cards",
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


def _run(source_facts: SourceFacts | None = None, **overrides: object):
    return preflight(
        agent=AGENT,
        to_host=DST,
        facts=_healthy_facts(**overrides),
        runtime=RUNTIME,
        required_ports=PORTS,
        source_facts=source_facts if source_facts is not None else _clean_source(),
        from_host=SRC,
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
    # Arrange
    report = preflight(agent=AGENT, to_host=DST, facts=TargetFacts(), runtime=RUNTIME)
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
