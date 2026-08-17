"""A QUEUED check is not a green check.

THE DEFECT (measured 2026-08-12). ``auto-merge-to-develop.yaml`` decided whether a
pull request was green with this filter::

    [ .statusCheckRollup[]
      | select(<advisory checks dropped>)
      | (.conclusion // .state // "")
      | select(. != "SUCCESS" and . != "NEUTRAL" and . != "SKIPPED" and . != "") ] | length

``gh`` reports ``conclusion`` as an EMPTY STRING -- not null, not absent -- for a
check that is QUEUED or IN_PROGRESS. ``select(. != "")`` therefore discarded
EXACTLY the checks meaning "we do not know yet", and the pending count came back
0. Run against a real capture of PR #985 carrying 8 queued checks (the fixture in
this directory), the old expression answered ``0`` -- fully green. The sweep then
merged with ``gh pr merge --admin``, which bypasses the branch protection that
would have refused it. Unverified code, merged automatically, silently.

It was not theoretical. An audit of 186 sweep merges found 6 that landed while a
non-advisory check was still running, and 3 of those (#316, #333, #334) were
outright RED -- all three pytest-matrix legs failed minutes after the merge.

The bug is not a typo. It is a MISSING THIRD STATE: a check is passing, failing,
or NOT YET KNOWN, and two buckets for three states put the third one in "green".

WHAT THESE TESTS PIN:

* a queued / in-progress check BLOCKS the merge, and is counted as "not yet
  known" rather than dropped;
* every neighbouring conclusion -- SKIPPED, NEUTRAL, CANCELLED, TIMED_OUT,
  ACTION_REQUIRED, STARTUP_FAILURE, STALE -- lands in a deliberate bucket;
* a state NOBODY HAS EVER SEEN blocks the merge. Unrecognised must never read as
  green, or this bug returns the next time GitHub adds a state;
* the verdict is reported as COUNTS PER STATE. An empty conclusion rendered into
  a table is a blank cell, and a blank cell reads to a human as "nothing wrong" --
  the same misreading in the report that the filter made in the code;
* the sweep does not pass ``--admin``, so GitHub's required checks are a second,
  independent guard.

NO MOCKS. These tests EXECUTE THE SHIPPED SHELL: the decision block is lifted
verbatim out of the real ``.github/workflows/auto-merge-to-develop.yaml`` by its
own anchors and run under ``bash``. Only ``rollup`` is supplied -- the value
``gh`` would have returned -- because the property under test is the DECISION, and
the decision is the code being run rather than a re-implementation of it. The two
JSON fixtures are verbatim ``gh pr view <n> --json statusCheckRollup`` captures:
one of PR #985 while its checks were queued, one of merged PR #964 fully green.

WHY THE CLASSIFICATION LIVES IN SHELL AND NOT IN jq: precisely so that this file
can run it. ``gh`` embeds its own jq and the CI runner has neither ``gh`` nor
``jq`` (see the workflow header), so a decision written in jq could only be
checked here by re-implementing it -- which would test the re-implementation.
jq's remaining job in the workflow is a pure projection of four raw fields, and
the tests that need a jq engine to check THAT are marked and skipped when no jq
is installed. The decision tests need nothing but bash.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-merge-to-develop.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The step that decides whether to merge, identified by the merge call itself
# rather than by its name, so renaming the step cannot silently empty this file.
MERGE_CALL = "gh pr merge"

# The greenness decision, delimited by two lines that exist for other reasons --
# the first statement of the counting loop, and the comment opening the block
# after it. Anchoring on load-bearing text rather than on markers added for the
# tests means the anchors cannot be deleted without deleting the code.
BLOCK_START = "n_pass=0"
BLOCK_END = "mergeStateStatus IS COMPUTED LAZILY"

# The synthetic PR number the extracted shell is run against.
PR_UNDER_TEST = "4242"

# Printed by the harness only if the shipped block let control through to the
# merge. Every "must not merge" assertion is this string being absent.
REACHED = "REACHED_MERGE"


def _merge_step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")) or {}
    steps = doc.get("jobs", {}).get("automerge", {}).get("steps", []) or []
    for step in steps:
        if MERGE_CALL in (step.get("run") or ""):
            return step
    raise AssertionError(
        f"no step in {WORKFLOW.name} contains {MERGE_CALL!r}. Either the sweep "
        "stopped merging (in which case delete this file) or the merge moved "
        "somewhere these tests no longer watch."
    )


def _script() -> str:
    return _merge_step()["run"]


def _greenness_block() -> str:
    """The shipped greenness decision, lifted verbatim and dedented."""
    lines = _script().splitlines()
    start = next((i for i, ln in enumerate(lines) if BLOCK_START in ln), None)
    if start is None:
        raise AssertionError(
            f"{WORKFLOW.name}: no line contains {BLOCK_START!r}. The greenness "
            "decision these tests execute has moved, been renamed, or been "
            "reverted to a form that does not count states. Repair the anchor "
            "rather than deleting the test, or the sweep goes unguarded."
        )
    end = next((i for i in range(start + 1, len(lines)) if BLOCK_END in lines[i]), None)
    if end is None:
        raise AssertionError(
            f"{WORKFLOW.name}: the block opening at line {start + 1} never "
            f"reaches {BLOCK_END!r}."
        )
    return textwrap.dedent("\n".join(lines[start:end]))


def _jq_filter() -> str:
    """The statusCheckRollup projection, verbatim, as the workflow ships it."""
    match = re.search(r"--json statusCheckRollup --jq '(.*?)'", _script(), re.DOTALL)
    if match is None:
        raise AssertionError(
            f"{WORKFLOW.name}: no `--json statusCheckRollup --jq '...'` call in "
            "the merge step. The projection these tests check has moved."
        )
    return match.group(1)


def _merge_lines() -> list[str]:
    """Every line of the shipped script that performs the merge."""
    lines = [ln.strip() for ln in _script().splitlines() if MERGE_CALL in ln]
    if not lines:
        raise AssertionError(
            f"{WORKFLOW.name} no longer calls {MERGE_CALL!r} -- this test can no "
            "longer see what flags the merge is performed with."
        )
    return lines


def _decide(rows, tmp_path) -> str:
    """Run the SHIPPED greenness decision under bash; return everything printed.

    ``rows`` are the TSV 4-tuples ``(status, conclusion, state, name)`` that the
    workflow's own jq projection produces. Everything between them and the
    verdict is the workflow's own code.

    The block is wrapped in a one-iteration ``for`` loop because every refusal in
    it ends in ``continue`` -- which IS the behaviour under test. If any of them
    fires, ``REACHED_MERGE`` is never printed.
    """
    rollup = "\n".join("\t".join(row) for row in rows)
    tsv = tmp_path / "rollup.tsv"
    tsv.write_text(rollup + "\n" if rollup else "", encoding="utf-8")

    harness = "\n".join(
        [
            "set -uo pipefail",
            f"pr={PR_UNDER_TEST}",
            'rollup="$(cat "$1")"',
            "for _once in 1; do",
            _greenness_block(),
            f'  echo "{REACHED}"',
            "done",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness, "bash", str(tsv)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"the extracted greenness block failed to run: rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def _counts(printed: str) -> tuple[int, int, int, int]:
    """(passing, failing, not-yet-known, total) as the sweep REPORTED them."""
    match = re.search(
        r"checks: (\d+) passing, (\d+) failing, (\d+) not yet known \(of (\d+)",
        printed,
    )
    if match is None:
        raise AssertionError(
            "the sweep did not report counts per state. That line is not "
            "decoration: a verdict rendered as a table of checks shows an empty "
            "conclusion as a BLANK CELL, and a blank cell reads as 'nothing "
            f"wrong'.\ngot:\n{printed}"
        )
    passing, failing, unknown, total = (int(group) for group in match.groups())
    return passing, failing, unknown, total


def _run_jq(jq_filter: str, payload_path: Path) -> str:
    """Run a jq filter over a real capture, refusing to guess on failure."""
    if not payload_path.exists():
        raise AssertionError(f"missing capture {payload_path}")
    proc = subprocess.run(
        ["jq", "-r", jq_filter],
        input=payload_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"jq failed on {payload_path.name}: rc={proc.returncode}\n{proc.stderr}"
        )
    return proc.stdout


def _project(payload_path: Path) -> list[tuple[str, ...]]:
    stdout = _run_jq(_jq_filter(), payload_path)
    return [tuple(line.split("\t")) for line in stdout.splitlines()]


requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is needed to run the shipped shell"
)
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="a jq engine is needed to run the shipped projection",
)


# ---------------------------------------------------------------------------
# Real captures. Verbatim `gh pr view <n> --json statusCheckRollup` output.
# ---------------------------------------------------------------------------
# The TSV below is what the workflow's own projection produces from those two
# files -- asserted, not assumed, by the jq tests further down, so the two
# representations cannot drift apart unnoticed.

QUEUED_PR985_FIXTURE = FIXTURES / "gh_status_check_rollup_queued_pr985.json"
GREEN_PR964_FIXTURE = FIXTURES / "gh_status_check_rollup_green_pr964.json"

# PR #985 at 2026-08-12T01:04Z: 8 queued checks, 1 finished. The old filter
# answered "0 pending" for exactly this input.
QUEUED_PR985_ROWS = [
    ("QUEUED", "", "", "rtd-sphinx-build-on-ubuntu-latest"),
    ("QUEUED", "", "", "import-smoke-on-ubuntu-py3-12"),
    ("QUEUED", "", "", "ruff-on-ubuntu-latest"),
    ("QUEUED", "", "", "no-hosted-runners-guard-on-self-hosted"),
    ("QUEUED", "", "", "scitex-dev-quality-audit-on-ubuntu-latest"),
    ("QUEUED", "", "", "pytest-matrix-on-ubuntu-py3.11"),
    ("COMPLETED", "SUCCESS", "", "CLAssistant"),
    ("QUEUED", "", "", "pytest-matrix-on-ubuntu-py3.12"),
    ("QUEUED", "", "", "pytest-matrix-on-ubuntu-py3.13"),
]

# Merged PR #964, every non-advisory check finished and green.
GREEN_PR964_ROWS = [
    ("COMPLETED", "SUCCESS", "", "CLAssistant"),
    ("COMPLETED", "SUCCESS", "", "CLAssistant"),
    ("COMPLETED", "SUCCESS", "", "rtd-sphinx-build-on-ubuntu-latest"),
    ("COMPLETED", "SUCCESS", "", "import-smoke-on-ubuntu-py3-12"),
    ("COMPLETED", "SUCCESS", "", "ruff-on-ubuntu-latest"),
    ("COMPLETED", "SUCCESS", "", "no-hosted-runners-guard-on-self-hosted"),
    ("COMPLETED", "SUCCESS", "", "scitex-dev-quality-audit-on-ubuntu-latest"),
    ("COMPLETED", "SUCCESS", "", "pytest-matrix-on-ubuntu-py3.11"),
    ("COMPLETED", "SUCCESS", "", "pytest-matrix-on-ubuntu-py3.12"),
    ("COMPLETED", "SUCCESS", "", "pytest-matrix-on-ubuntu-py3.13"),
]

# The old filter, kept verbatim as the historical artefact it is. It is never
# used to decide anything -- only to demonstrate, against the real capture, what
# it used to answer.
OLD_BUGGY_FILTER = (
    "[ .statusCheckRollup[]\n"
    '  | select(((.name // .context // "") | ascii_downcase '
    '| test("codecov|readthedocs")) | not)\n'
    '  | (.conclusion // .state // "")\n'
    '  | select(. != "SUCCESS" and . != "NEUTRAL" and . != "SKIPPED" '
    'and . != "") ] | length'
)

# (status, conclusion, state) -> (bucket, may the sweep merge on it alone?)
# Every one of these is a deliberate decision; see the workflow's own comments
# for the reasoning, SKIPPED especially.
ONE_CHECK_CASES = [
    # A verdict was reported and it was positive.
    (("COMPLETED", "SUCCESS", ""), "pass", True),
    (("COMPLETED", "NEUTRAL", ""), "pass", True),
    (("COMPLETED", "SKIPPED", ""), "pass", True),
    # A verdict was reported and it was negative.
    (("COMPLETED", "FAILURE", ""), "fail", False),
    (("COMPLETED", "TIMED_OUT", ""), "fail", False),
    (("COMPLETED", "ACTION_REQUIRED", ""), "fail", False),
    (("COMPLETED", "STARTUP_FAILURE", ""), "fail", False),
    # It ran, but produced no verdict worth trusting.
    (("COMPLETED", "CANCELLED", ""), "unknown", False),
    (("COMPLETED", "STALE", ""), "unknown", False),
    # THE BUG: no verdict yet, reported by gh as an empty conclusion.
    (("QUEUED", "", ""), "unknown", False),
    (("IN_PROGRESS", "", ""), "unknown", False),
    (("WAITING", "", ""), "unknown", False),
    (("REQUESTED", "", ""), "unknown", False),
    (("PENDING", "", ""), "unknown", False),
    # Completed, yet said nothing. Should not happen; we do not guess.
    (("COMPLETED", "", ""), "unknown", False),
    # The older StatusContext shape, which reports `state` and no `status`.
    (("", "", "SUCCESS"), "pass", True),
    (("", "", "PENDING"), "unknown", False),
    (("", "", "EXPECTED"), "unknown", False),
    (("", "", "ERROR"), "fail", False),
    (("", "", "FAILURE"), "fail", False),
    # A state nobody has ever seen. THIS is the arm that keeps the bug from
    # coming back the next time GitHub invents a conclusion.
    (("COMPLETED", "WORMHOLE_2031", ""), "unknown", False),
    (("QUANTUM_SUPERPOSITION", "", ""), "unknown", False),
    # GitHub's REST API lower-cases these; GraphQL upper-cases them. Reading a
    # lower-cased green as "unrecognised" would be safe but would stall the
    # sweep, so it is normalised rather than left to chance.
    (("completed", "success", ""), "pass", True),
]
ONE_CHECK_IDS = [
    f"{st or '-'}/{cc or '-'}/{sc or '-'}" for (st, cc, sc), _, _ in ONE_CHECK_CASES
]


# ---------------------------------------------------------------------------
# Guard the guard.
# ---------------------------------------------------------------------------


def test_the_greenness_decision_is_findable():
    """If the decision moved, every test below would pass without checking it."""
    # Arrange
    workflow = WORKFLOW

    # Act
    block = _greenness_block()

    # Assert
    assert block.strip(), (
        f"{workflow} has no greenness decision between {BLOCK_START!r} and "
        f"{BLOCK_END!r} -- every test in this file would be vacuous."
    )


def test_the_three_states_are_named_in_the_shipped_code():
    """Three buckets, not two. The bug was the third one having nowhere to go."""
    # Arrange
    block = _greenness_block()

    # Act
    counters = [name for name in ("n_pass", "n_fail", "n_unknown") if name in block]

    # Assert
    assert len(counters) == 3, (
        "the shipped decision does not count three separate states (found "
        f"{counters}). A check is passing, failing, or NOT YET KNOWN; collapsing "
        "the third into either pole is the 2026-08-12 defect itself."
    )


# ---------------------------------------------------------------------------
# A. The defect: a queued check must block the merge.
# ---------------------------------------------------------------------------


@requires_bash
def test_a_queued_check_blocks_the_merge(tmp_path):
    """THE PROPERTY THE INCIDENT NEEDED, on the real capture that exposed it."""
    # Arrange
    rows = QUEUED_PR985_ROWS

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert REACHED not in printed, (
        "a pull request with 8 QUEUED checks reached the merge. This is the "
        "2026-08-12 defect exactly: gh reports a queued check's conclusion as "
        "an empty string, and the sweep read that as green, then merged with "
        f"--admin past the protection that would have refused it.\n{printed}"
    )


@requires_bash
def test_queued_checks_are_counted_as_not_yet_known(tmp_path):
    """Not merely blocked -- COUNTED, in the state that says why."""
    # Arrange
    expected_unknown = sum(1 for row in QUEUED_PR985_ROWS if row[0] == "QUEUED")

    # Act
    printed = _decide(QUEUED_PR985_ROWS, tmp_path)
    passing, failing, unknown, total = _counts(printed)

    # Assert
    assert (passing, failing, unknown, total) == (1, 0, expected_unknown, 9), (
        f"the sweep counted {passing} passing / {failing} failing / {unknown} "
        f"not-yet-known of {total}, but the real capture holds 1 finished check "
        f"and {expected_unknown} queued ones. The old filter answered 0 pending "
        f"for this same input.\n{printed}"
    )


@requires_bash
def test_an_all_green_pull_request_still_merges(tmp_path):
    """The fix must not stall the sweep -- a sweep that never merges is an outage."""
    # Arrange
    rows = GREEN_PR964_ROWS

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert REACHED in printed, (
        "a fully green pull request (the real capture of merged PR #964) did "
        "NOT reach the merge. Refusing everything is not safety; this file "
        f"exists because 38 green PRs once sat unmerged for 8 days.\n{printed}"
    )


@requires_bash
def test_one_queued_check_among_many_green_ones_still_blocks(tmp_path):
    """The dangerous shape: almost everything finished, so the log looks fine."""
    # Arrange
    rows = list(GREEN_PR964_ROWS) + [("QUEUED", "", "", "the-slow-one")]

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert REACHED not in printed, (
        "ten green checks and one still queued was treated as green. The "
        "saturated self-hosted pool makes this the COMMON shape, not a corner "
        f"case.\n{printed}"
    )


@requires_bash
def test_one_queued_check_among_many_green_ones_is_counted(tmp_path):
    """And the one that is not known must be visible in the counts."""
    # Arrange
    rows = list(GREEN_PR964_ROWS) + [("QUEUED", "", "", "the-slow-one")]

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert _counts(printed) == (10, 0, 1, 11), (
        "ten passing and one queued was not reported as 10/0/1. The count is "
        "how a reader tells 'green' from 'not finished yet' at a glance.\n"
        f"{printed}"
    )


# ---------------------------------------------------------------------------
# B. The neighbouring states, each decided on purpose.
# ---------------------------------------------------------------------------


@requires_bash
@pytest.mark.parametrize("fields, bucket, may_merge", ONE_CHECK_CASES, ids=ONE_CHECK_IDS)
def test_each_check_state_lands_in_its_deliberate_bucket(
    fields, bucket, may_merge, tmp_path
):
    """SKIPPED passes, CANCELLED does not, and nothing lands in green by default."""
    # Arrange
    status, conclusion, state = fields
    rows = [(status, conclusion, state, "the-only-check")]

    # Act
    printed = _decide(rows, tmp_path)
    passing, failing, unknown, _total = _counts(printed)

    # Assert
    assert {"pass": passing, "fail": failing, "unknown": unknown}[bucket] == 1, (
        f"status={status!r} conclusion={conclusion!r} state={state!r} was "
        f"expected in the {bucket!r} bucket, but the sweep counted "
        f"{passing} passing / {failing} failing / {unknown} not-yet-known.\n"
        f"{printed}"
    )


@requires_bash
@pytest.mark.parametrize("fields, bucket, may_merge", ONE_CHECK_CASES, ids=ONE_CHECK_IDS)
def test_each_check_state_decides_the_merge_the_same_way(
    fields, bucket, may_merge, tmp_path
):
    """The bucket is only worth counting if it also governs the merge."""
    # Arrange
    status, conclusion, state = fields
    rows = [(status, conclusion, state, "the-only-check")]

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert (REACHED in printed) is may_merge, (
        f"status={status!r} conclusion={conclusion!r} state={state!r} is bucketed "
        f"as {bucket!r}, so the merge should be reached={may_merge}; it was "
        f"{REACHED in printed}.\n{printed}"
    )


@requires_bash
def test_an_unrecognised_state_is_never_green(tmp_path):
    """The default arm is the whole defence against this bug returning."""
    # Arrange
    invented = ("COMPLETED", "SOME_STATE_GITHUB_ADDS_IN_2031", "", "future-check")

    # Act
    printed = _decide([invented], tmp_path)
    passing, _failing, unknown, _total = _counts(printed)

    # Assert
    assert REACHED not in printed and (passing, unknown) == (0, 1), (
        "a conclusion this code has never seen was treated as mergeable. An "
        "unrecognised state must fall into 'not yet known', never into 'green' "
        f"-- otherwise GitHub adding a state silently re-arms the bug.\n{printed}"
    )


# ---------------------------------------------------------------------------
# C. Reporting: an empty state must never render as a blank.
# ---------------------------------------------------------------------------


@requires_bash
def test_an_absent_state_is_named_not_left_blank(tmp_path):
    """The mistake in the report is the same mistake as in the code."""
    # Arrange
    silent = ("COMPLETED", "", "", "check-that-said-nothing")

    # Act
    printed = _decide([silent], tmp_path)

    # Assert
    assert "NO STATE REPORTED" in printed, (
        "a check with an empty conclusion AND an empty state was reported with "
        "nothing where its state should be. A blank renders as 'nothing wrong' "
        f"to a human reading the log -- give it a name.\n{printed}"
    )


@requires_bash
def test_the_counts_are_reported_even_when_everything_is_green(tmp_path):
    """Counts on every tick, not only on refusals -- silence is not evidence."""
    # Arrange
    rows = GREEN_PR964_ROWS

    # Act
    printed = _decide(rows, tmp_path)

    # Assert
    assert _counts(printed) == (10, 0, 0, 10), (
        "a fully green pull request did not get its counts reported.\n"
        f"{printed}"
    )


@requires_bash
def test_a_pull_request_with_no_checks_at_all_is_refused(tmp_path):
    """Absence of evidence is not evidence of green."""
    # Arrange
    nothing_ran: list[tuple[str, str, str, str]] = []

    # Act
    printed = _decide(nothing_ran, tmp_path)

    # Assert
    assert REACHED not in printed, (
        "a pull request whose workflows produced NO checks at all was merged. "
        "Nothing has tested it; with --admin that merge would also have gone "
        f"past the protection that says so.\n{printed}"
    )


@requires_bash
def test_the_no_checks_refusal_is_a_github_annotation(tmp_path):
    """Loud means it survives into the run summary, not just the raw log."""
    # Arrange
    nothing_ran: list[tuple[str, str, str, str]] = []

    # Act
    printed = _decide(nothing_ran, tmp_path)

    # Assert
    assert "::warning::" in printed, (
        "the refusal of an untested pull request is not a GitHub annotation, so "
        f"it does not surface in the run summary.\n{printed}"
    )


# ---------------------------------------------------------------------------
# D. The projection, against the real gh payloads. Needs a jq engine.
# ---------------------------------------------------------------------------


@requires_jq
@pytest.mark.parametrize(
    "fixture, rows",
    [
        (QUEUED_PR985_FIXTURE, QUEUED_PR985_ROWS),
        (GREEN_PR964_FIXTURE, GREEN_PR964_ROWS),
    ],
    ids=["queued-pr985", "green-pr964"],
)
def test_the_projection_turns_real_gh_output_into_those_rows(fixture, rows):
    """Ties the TSV the decision tests use back to the real payload it came from."""
    # Arrange
    capture = fixture

    # Act
    projected = _project(capture)

    # Assert
    assert projected == rows, (
        f"the shipped jq projection no longer turns {capture.name} into the "
        "rows the decision tests are written against, so those tests are now "
        f"checking a shape gh does not produce.\ngot: {projected}"
    )


@requires_jq
def test_gh_still_reports_an_empty_conclusion_for_a_queued_check():
    """The premise of the whole bug, re-measured against the real capture."""
    # Arrange
    payload = json.loads(QUEUED_PR985_FIXTURE.read_text(encoding="utf-8"))

    # Act
    queued = [e for e in payload["statusCheckRollup"] if e.get("status") == "QUEUED"]

    # Assert
    assert queued and all(e.get("conclusion") == "" for e in queued), (
        "gh no longer reports an EMPTY STRING conclusion for a queued check (or "
        "the capture holds no queued checks). That premise is what made "
        '`select(. != "")` drop pending checks; if it has changed, re-read the '
        "workflow's comments before trusting them."
    )


@requires_jq
def test_the_old_predicate_called_the_real_queued_capture_green():
    """The mutation, measured: what the shipped code used to answer, and why.

    This is the historical record, not a guard. It asserts that the OLD filter
    answers "0 pending" for a capture holding 8 queued checks -- i.e. that the
    bug was real and this fixture reproduces it. The guard is
    ``test_a_queued_check_blocks_the_merge`` above, which fails if the shipped
    code ever answers the same way again.
    """
    # Arrange
    capture = QUEUED_PR985_FIXTURE

    # Act
    answered = _run_jq(OLD_BUGGY_FILTER, capture).strip()

    # Assert
    assert answered == "0", (
        "the old filter no longer answers 0 for this capture, so this fixture "
        "no longer reproduces the defect it was captured to document; either "
        f"the capture or this record needs repairing.\ngot: {answered!r}"
    )


# ---------------------------------------------------------------------------
# E. The second guard.
# ---------------------------------------------------------------------------


def test_the_sweep_does_not_merge_with_admin():
    """--admin is what let one bad predicate merge past branch protection."""
    # Arrange
    merge_lines = _merge_lines()

    # Act
    offenders = [line for line in merge_lines if "--admin" in line]

    # Assert
    assert not offenders, (
        "the sweep merges with --admin, which bypasses branch protection and "
        "removes the only guard independent of the predicate above. develop "
        "requires two pytest contexts and ZERO approving reviews, so dropping "
        "the flag costs a genuinely green PR nothing. If a required check that "
        "never reports has since made --admin necessary, say which one in the "
        f"workflow and update this test deliberately.\n{offenders}"
    )
