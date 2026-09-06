"""Tests for the silent-block detector behind ``sac ci blocked``.

THE REGRESSION THESE EXIST FOR, measured on this repo 2026-08-12 08:49Z:
#1014, #1017 and #1005 were all ``MERGEABLE / BLOCKED`` with ``fail=0`` because
the required ``pytest-matrix-on-ubuntu-py3.13`` leg had never been given a
runner. A check that never STARTED is absent from ``statusCheckRollup``
entirely, so every count of the rollup said green while the PRs could not
merge. The signal is a required NAME that is missing — which is exactly what a
count cannot see.

So the load-bearing test is :func:`test_required_context_absent_from_rollup_is_silently_blocked`:
if that ever passes on a rollup-derived required list, the detector has become
a tautology and can only return green.

The ``run_gh`` injection seam (the same one ``_ci_why`` / ``_ci_runners`` tests
use) stands in for the network. AAA, one assertion per test.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.cli_pkg._ci_blocked import (
    FAILED,
    NEVER_STARTED,
    PASSED,
    PENDING,
    PRGate,
    RequiredContext,
    audit_blocked,
    render_text,
    required_contexts,
)
from scitex_agent_container.cli_pkg._ci_why import CIWhyError

REQUIRED = ["pytest-matrix-on-ubuntu-py3.11", "pytest-matrix-on-ubuntu-py3.13"]


def _check(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _pr(
    rollup,
    number=1014,
    base="develop",
    draft=False,
    state="BLOCKED",
    mergeable="MERGEABLE",
):
    return {
        "number": number,
        "title": "build(deps): restore the scitex-dev ceiling",
        "baseRefName": base,
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "statusCheckRollup": rollup,
        "isDraft": draft,
        "url": f"https://example/pull/{number}",
    }


def _gh(prs, contexts=REQUIRED, protection_raises=False, protection_body=None):
    """A gh seam answering both `pr list` and the branch-protection API.

    ``protection_body`` models the case that actually happens in production and
    that ``protection_raises`` does NOT: ``gh api`` prints a 404/403 error body
    to STDOUT and exits non-zero, and ``run_gh`` returns stdout whenever it is
    non-empty. So an unreadable branch arrives here as parseable JSON, not as
    an exception.
    """

    def _router(argv: list[str]) -> str:
        if argv and argv[0] == "pr":
            return json.dumps(prs)
        if argv and argv[0] == "api":
            if protection_raises:
                raise CIWhyError("404 Not Found")
            if protection_body is not None:
                return json.dumps(protection_body)
            return json.dumps({"strict": False, "contexts": contexts})
        raise AssertionError(f"unexpected gh call: {argv}")

    return _router


# ---------------------------------------------------------------------------
# The pathology itself.
# ---------------------------------------------------------------------------


def test_required_context_absent_from_rollup_is_silently_blocked():
    # Arrange — py3.11 reported green; py3.13 never started, so it is ABSENT.
    rollup = [
        _check("pytest-matrix-on-ubuntu-py3.11"),
        _check("ruff-on-ubuntu-latest"),
    ]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].silently_blocked is True


def test_absent_required_context_is_named_not_merely_counted():
    # Arrange
    rollup = [_check("pytest-matrix-on-ubuntu-py3.11")]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert [c.name for c in gates[0].never_started] == [
        "pytest-matrix-on-ubuntu-py3.13"
    ]


def test_naive_pending_count_reads_zero_on_the_blocked_pr():
    # Arrange — the whole reason a rollup count cannot find this.
    rollup = [_check("pytest-matrix-on-ubuntu-py3.11")]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].pending == []


def test_all_required_contexts_green_is_not_flagged():
    # Arrange
    rollup = [_check(n) for n in REQUIRED]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].silently_blocked is False


# ---------------------------------------------------------------------------
# The states that are NOT this bug.
# ---------------------------------------------------------------------------


def test_queued_required_context_is_pending_not_never_started():
    # Arrange — present in the rollup with no verdict yet: visible, benign.
    rollup = [
        _check("pytest-matrix-on-ubuntu-py3.11"),
        {
            "name": "pytest-matrix-on-ubuntu-py3.13",
            "status": "QUEUED",
            "conclusion": None,
        },
    ]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].required[1].state == PENDING


def test_a_pending_required_context_alone_is_not_a_silent_block():
    # Arrange
    rollup = [
        _check("pytest-matrix-on-ubuntu-py3.11"),
        {
            "name": "pytest-matrix-on-ubuntu-py3.13",
            "status": "IN_PROGRESS",
            "conclusion": None,
        },
    ]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].silently_blocked is False


def test_a_failing_required_check_is_loud_so_never_flagged_silent():
    # Arrange — red PRs already alarm; this detector must not double-report.
    rollup = [
        _check("pytest-matrix-on-ubuntu-py3.11", conclusion="FAILURE"),
    ]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].silently_blocked is False


def test_a_conflicting_pr_is_never_flagged():
    # Arrange — measured live 2026-08-12: #883 and #942 were CONFLICTING/DIRTY
    # and their matrix legs had never enqueued BECAUSE of the conflict. That
    # blocker is already named in `mergeable`; a second alarm is noise.
    rollup = []

    # Act
    gates = audit_blocked(
        run_gh=_gh([_pr(rollup, mergeable="CONFLICTING", state="DIRTY")])
    )

    # Assert
    assert gates[0].silently_blocked is False


def test_a_conflicting_pr_still_reports_its_missing_contexts():
    # Arrange — excluded from the ALARM, not hidden from the REPORT.
    rollup = []

    # Act
    gates = audit_blocked(
        run_gh=_gh([_pr(rollup, mergeable="CONFLICTING", state="DIRTY")])
    )

    # Assert
    assert len(gates[0].never_started) == 2


def test_a_draft_pr_is_never_flagged():
    # Arrange — a draft is MEANT to be unmergeable; flagging it cries wolf.
    rollup = [_check("pytest-matrix-on-ubuntu-py3.11")]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup, draft=True)]))

    # Assert
    assert gates[0].silently_blocked is False


@pytest.mark.parametrize("conclusion", ["NEUTRAL", "SKIPPED"])
def test_conclusions_github_treats_as_passing_are_passed(conclusion):
    # Arrange
    rollup = [_check(n, conclusion=conclusion) for n in REQUIRED]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].required[0].state == PASSED


def test_a_rerun_green_does_not_hide_an_earlier_red_leg():
    # Arrange — same name twice; the worst verdict must win.
    rollup = [
        _check("pytest-matrix-on-ubuntu-py3.11", conclusion="FAILURE"),
        _check("pytest-matrix-on-ubuntu-py3.11", conclusion="SUCCESS"),
        _check("pytest-matrix-on-ubuntu-py3.13"),
    ]

    # Act
    gates = audit_blocked(run_gh=_gh([_pr(rollup)]))

    # Assert
    assert gates[0].required[0].state == FAILED


# ---------------------------------------------------------------------------
# Required-context resolution.
# ---------------------------------------------------------------------------


def test_required_contexts_reads_the_protection_contexts_list():
    # Arrange
    gh = _gh([], contexts=REQUIRED)

    # Act
    names = required_contexts("develop", run_gh=gh)

    # Assert
    assert names == REQUIRED


def test_required_contexts_also_reads_the_newer_checks_form():
    # Arrange — the `checks[]` shape carries the same names plus an app_id.
    def gh(argv):
        return json.dumps({"checks": [{"context": "c1", "app_id": 15368}]})

    # Act
    names = required_contexts("develop", run_gh=gh)

    # Assert
    assert names == ["c1"]


UNPROTECTED_BODY = {"message": "Branch not protected", "status": "404"}
UNREADABLE_BODY = {"message": "Resource not accessible by integration", "status": "403"}


def test_an_unprotected_base_requires_nothing_and_is_not_flagged():
    # Arrange — GitHub's real "no protection" answer: a 404 BODY on stdout.
    gh = _gh([_pr([])], protection_body=UNPROTECTED_BODY)

    # Act
    gates = audit_blocked(run_gh=gh)

    # Assert
    assert gates[0].silently_blocked is False


def test_unreadable_protection_raises_rather_than_reading_clean():
    """THE BUG. A 403 body parsed as data yielded [] -> nothing required ->
    nothing can be NEVER_STARTED -> `silently_blocked` False -> CLEAN.

    Not-permitted-to-look is not the same fact as nothing-is-required, and only
    one of them is a verdict.
    """
    # Arrange
    gh = _gh([_pr([])], protection_body=UNREADABLE_BODY)

    # Act
    with pytest.raises(CIWhyError) as excinfo:
        audit_blocked(run_gh=gh)

    # Assert
    assert "could not read branch protection" in str(excinfo.value)


def test_a_hard_protection_failure_propagates_rather_than_reading_clean():
    """The sibling route to the same wrong verdict: `run_gh` raising was
    swallowed here, which also defeated the CORRECT guard in `sac ci blocked`
    ("UNKNOWN is not green"). That guard could not fire for this path.
    """
    # Arrange
    gh = _gh([_pr([])], protection_raises=True)

    # Act
    with pytest.raises(CIWhyError) as excinfo:
        audit_blocked(run_gh=gh)

    # Assert
    assert excinfo.value is not None


def test_base_filter_selects_only_matching_prs():
    # Arrange
    prs = [_pr([], number=1, base="develop"), _pr([], number=2, base="main")]

    # Act
    gates = audit_blocked(run_gh=_gh(prs), base="main")

    # Assert
    assert [g.number for g in gates] == [2]


def test_unparseable_pr_list_raises_rather_than_reporting_clean():
    # Arrange — UNKNOWN must never degrade into "nothing blocked".
    def gh(argv):
        return "not json"

    # Act
    call = lambda: audit_blocked(run_gh=gh)  # noqa: E731

    # Assert
    with pytest.raises(CIWhyError):
        call()


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def test_render_marks_the_silently_blocked_pr():
    # Arrange
    gates = audit_blocked(run_gh=_gh([_pr([_check(REQUIRED[0])])]))

    # Act
    text = render_text(gates)

    # Assert
    assert "SILENT-BLOCK" in text


def test_render_says_so_when_nothing_is_silently_blocked():
    # Arrange
    gates = audit_blocked(run_gh=_gh([_pr([_check(n) for n in REQUIRED])]))

    # Act
    text = render_text(gates)

    # Assert
    assert "no PR is blocked by a never-started required check" in text


def test_render_handles_no_open_prs():
    # Arrange
    gates: list[PRGate] = []

    # Act
    text = render_text(gates)

    # Assert
    assert text == "no open PRs"


def test_to_dict_exposes_the_missing_name_for_machine_consumers():
    # Arrange
    gates = audit_blocked(run_gh=_gh([_pr([_check(REQUIRED[0])])]))

    # Act
    payload = gates[0].to_dict()

    # Assert
    assert payload["never_started"] == ["pytest-matrix-on-ubuntu-py3.13"]


def test_required_context_is_silent_property():
    # Arrange
    ctx = RequiredContext(name="x", state=NEVER_STARTED)

    # Act
    silent = ctx.is_silent

    # Assert
    assert silent is True


def test_pr_gate_with_no_required_contexts_is_not_flagged():
    # Arrange
    gate = PRGate(
        number=1,
        title="t",
        base="develop",
        merge_state_status="BLOCKED",
        mergeable="MERGEABLE",
    )

    # Act
    flagged = gate.silently_blocked

    # Assert
    assert flagged is False
