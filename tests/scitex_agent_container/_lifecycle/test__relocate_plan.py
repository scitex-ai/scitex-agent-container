"""An unreachable host is ONE problem, not eleven, and the list must say so.

Two failures of a complete-but-unreadable report are tested here, both of them
things the operator asked for on 2026-08-11 (「なるべく多くのヒントを1回で出す」 —
as many hints as possible in one pass):

    ROOT CAUSES   every fact gathered through a failed transport carries the SAME
                  reason string, so identical reasons ARE one cause. Printing
                  eleven unknowns buries the one thing to fix inside its own
                  consequences.
    ORDER         a list ordered by check index is the code's structure leaking
                  into the operator's afternoon. He works by action: what the
                  target must be given, what has to travel, what is a spec edit,
                  what is still unmeasured.

A check swallowed by a root cause is never dropped and never downgraded — it is
named under the cause, and it still blocks.

Pure: a report and an errors dict in, dataclasses out. No I/O, no mocks.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_plan import (
    ACTION_CARRY,
    ACTION_MEASURE,
    ACTION_PROVISION,
    ACTION_SPEC,
    build_plan,
)
from scitex_agent_container._lifecycle._relocate_preflight import (
    CHECK_BINDS,
    CHECK_IMAGE,
    CHECK_PORTS,
    Check,
    PreflightReport,
)

AGENT = "scitex-hpc"
DST = "scitex-compute-04"
SRC = "ywata-note-win"
WORKDIR = "/home/ywatanabe/proj/paper-scitex-clew"

TRANSPORT_FAILURE = "ProbeTransportError: could not run the probe on the host"


def _report(*checks: Check) -> PreflightReport:
    return PreflightReport(agent=AGENT, to_host=DST, checks=checks)


def _unknown(name: str) -> Check:
    return Check(name=name, ok=None, detail=f"{name} was not observed", hint="measure it")


def _fail(name: str, detail: str = "broken") -> Check:
    return Check(name=name, ok=False, detail=detail, hint="fix it")


# ---------------------------------------------------------------------------
# root causes — one unreachable host is one problem
# ---------------------------------------------------------------------------


def test_unknowns_sharing_a_probe_failure_become_one_root_cause() -> None:
    # Arrange: the shape of every unreachable target.
    report = _report(_unknown(CHECK_IMAGE), _unknown(CHECK_PORTS))
    errors = {"image_present": TRANSPORT_FAILURE, "ports_in_use": TRANSPORT_FAILURE}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert len(plan.causes) == 1


def test_a_root_cause_names_every_check_it_blocked() -> None:
    # Arrange: swallowed is not dropped — the reader must still see what is
    # unanswered, or a fixed transport is followed by a surprise.
    report = _report(_unknown(CHECK_IMAGE), _unknown(CHECK_PORTS))
    errors = {"image_present": TRANSPORT_FAILURE, "ports_in_use": TRANSPORT_FAILURE}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert set(plan.causes[0].checks) == {CHECK_IMAGE, CHECK_PORTS}


def test_a_root_caused_check_is_not_also_listed_as_its_own_problem() -> None:
    # Arrange: presenting four consequences as four tasks is worse than
    # presenting one cause.
    report = _report(_unknown(CHECK_IMAGE), _unknown(CHECK_PORTS))
    errors = {"image_present": TRANSPORT_FAILURE, "ports_in_use": TRANSPORT_FAILURE}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert plan.items == ()


def test_unknowns_with_different_reasons_are_never_merged() -> None:
    # Arrange: two genuinely independent problems. Merging them would hide one.
    report = _report(_unknown(CHECK_IMAGE), _unknown(CHECK_PORTS))
    errors = {"image_present": "no image declared", "ports_in_use": "no ss on target"}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert plan.causes == ()


def test_independent_unknowns_are_listed_individually() -> None:
    # Arrange
    report = _report(_unknown(CHECK_IMAGE), _unknown(CHECK_PORTS))
    errors = {"image_present": "no image declared", "ports_in_use": "no ss on target"}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert len(plan.items) == 2


def test_a_lone_unknown_is_not_promoted_into_a_root_cause() -> None:
    # Arrange: a "cause" with one member is just the problem, said twice.
    report = _report(_unknown(CHECK_IMAGE))
    errors = {"image_present": TRANSPORT_FAILURE}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert plan.causes == ()


def test_an_unknown_carries_its_probe_error_into_the_item() -> None:
    # Arrange: knowing a fact is missing without knowing why turns a five-second
    # fix into an investigation.
    report = _report(_unknown(CHECK_IMAGE))
    errors = {"image_present": "SSHTimeout: after 60s"}
    # Act
    plan = build_plan(report, errors=errors)
    # Assert
    assert "SSHTimeout" in plan.items[0].what


# ---------------------------------------------------------------------------
# order — by what to DO, not by check index
# ---------------------------------------------------------------------------


def test_provisioning_comes_before_spec_edits() -> None:
    # Arrange: deliberately supplied in the opposite order.
    report = _report(
        _fail("runtime_supported"),
        _fail(CHECK_IMAGE),
    )
    # Act
    plan = build_plan(report)
    # Assert
    assert [a for a, _ in plan.by_action()] == [ACTION_PROVISION, ACTION_SPEC]


def test_unknowns_are_ordered_last_because_they_are_questions_not_tasks() -> None:
    # Arrange
    report = _report(_unknown(CHECK_PORTS), _fail(CHECK_IMAGE))
    # Act
    plan = build_plan(report)
    # Assert
    assert [a for a, _ in plan.by_action()] == [ACTION_PROVISION, ACTION_MEASURE]


def test_source_side_work_is_ordered_as_something_that_must_travel() -> None:
    # Arrange: uncommitted work is not a target problem; it is cargo.
    report = _report(_fail("source_work_committed"), _fail(CHECK_IMAGE))
    # Act
    plan = build_plan(report, from_host=SRC)
    # Assert
    assert [a for a, _ in plan.by_action()] == [ACTION_PROVISION, ACTION_CARRY]


def test_a_source_side_item_names_the_source_host_not_the_target() -> None:
    # Arrange: a check is meaningless without its vantage point, and this one was
    # measured on the machine being LEFT.
    report = _report(_fail("source_work_committed"))
    # Act
    plan = build_plan(report, from_host=SRC)
    # Assert
    assert plan.items[0].where == SRC


def test_a_target_side_item_names_the_target_host() -> None:
    # Arrange
    report = _report(_fail(CHECK_IMAGE))
    # Act
    plan = build_plan(report)
    # Assert
    assert plan.items[0].where == DST


# ---------------------------------------------------------------------------
# binds — ONE check, several actions
# ---------------------------------------------------------------------------


def test_a_mixed_bind_failure_produces_items_in_two_different_buckets() -> None:
    # Arrange: the 2026-08-11 fleet shape — infrastructure and agent data absent
    # from the same host, needing opposite fixes.
    detail = f"bind sources absent on {DST}: /mnt/c, {WORKDIR}/runs/x"
    report = _report(_fail(CHECK_BINDS, detail))
    # Act
    plan = build_plan(report, workdir=WORKDIR, from_host=SRC)
    # Assert
    assert [a for a, _ in plan.by_action()] == [ACTION_PROVISION, ACTION_CARRY]


def test_each_bind_item_carries_its_own_path() -> None:
    # Arrange
    detail = f"bind sources absent on {DST}: /mnt/c, {WORKDIR}/runs/x"
    report = _report(_fail(CHECK_BINDS, detail))
    # Act
    plan = build_plan(report, workdir=WORKDIR, from_host=SRC)
    # Assert
    assert {i.what.split(" ")[0] for i in plan.items} == {"/mnt/c", f"{WORKDIR}/runs/x"}


def test_each_bind_item_carries_a_fix_naming_that_path() -> None:
    # Arrange: the deliverable is the hint, and a hint that does not name the
    # path it is about is a paragraph, not an instruction.
    detail = f"bind sources absent on {DST}: /mnt/c"
    report = _report(_fail(CHECK_BINDS, detail))
    # Act
    plan = build_plan(report, workdir=WORKDIR, from_host=SRC)
    # Assert
    assert "/mnt/c" in plan.items[0].fix


# ---------------------------------------------------------------------------
# the empty case
# ---------------------------------------------------------------------------


def test_a_clean_report_produces_no_plan_at_all() -> None:
    # Arrange
    report = _report(Check(name=CHECK_IMAGE, ok=True, detail="image present"))
    # Act
    plan = build_plan(report)
    # Assert
    assert plan.empty is True


def test_every_item_carries_a_verdict_word() -> None:
    # Arrange: FAIL and UNKNOWN call for different actions and must not blur.
    report = _report(_fail(CHECK_IMAGE), _unknown(CHECK_PORTS))
    # Act
    plan = build_plan(report)
    # Assert
    assert {i.verdict for i in plan.items} == {"FAIL", "UNKNOWN"}
