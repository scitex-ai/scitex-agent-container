"""A held PR must not be auto-merged, and an auto-merge must admit it was one.

THE INCIDENT (2026-08-12). An agent deliberately HELD a pull request: it relaxed a
guard the operator had escalated, and the agent judged that a human had to look at
it first. It was merged anyway -- unreviewed, uncommented, at 00:29Z. Two separate
defects produced that, and they fail independently:

1. THE HOLD EXISTED ONLY IN ONE AGENT'S HEAD. Nothing in the repository could have
   stopped the merge, because nothing in the repository knew a hold had been
   declared. An intention that no mechanism reads is not a hold.

2. AN AUTOMATED MERGE WAS INDISTINGUISHABLE FROM A HUMAN ONE. Every agent, every
   workflow and the operator act through the same GitHub account, so ``merged_by``
   cannot say whether anybody read the diff. The agent that held the PR could not
   find out who had overridden it either.

``auto-merge-to-develop.yaml`` is this repo's merge automation -- a cron sweep that
merges green PRs targeting ``develop`` with ``gh pr merge --admin``. These tests
pin the two properties that close the defects above:

* a HOLD MARKER (a ``hold`` / ``do-not-merge`` label, or draft status) makes the
  sweep refuse the PR, LOUDLY, on every tick, before it can reach the merge; and
* the sweep POSTS A COMMENT NAMING ITSELF BEFORE it merges, and does not merge at
  all if that comment cannot be posted.

NO MOCKS. The hold-decision tests EXECUTE THE SHIPPED SHELL -- extracted verbatim
from the real ``.github/workflows/auto-merge-to-develop.yaml`` by its own anchors
and run under ``bash``. Only the label string is supplied, the value ``gh`` would
have returned, because the property under test is the DECISION and the decision is
the code being run, not a re-implementation of it. The remaining tests assert ORDER
within that same real file: a check that runs after the merge is not a gate.

Why a guard rather than a review comment: the workflow is read from the DEFAULT
branch, so nobody ever exercises this logic on a PR. A regression here would land
silently and only surface the next time somebody's hold was ignored.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-merge-to-develop.yaml"

# The step that decides whether to merge, identified by the merge call itself
# rather than by its name, so renaming the step cannot silently empty this file.
MERGE_CALL = "gh pr merge"

# The synthetic PR number the extracted shell is run against.
PR_UNDER_TEST = "4242"


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


def _env() -> dict:
    return _merge_step().get("env", {}) or {}


def _at(needle: str) -> int:
    """Character offset of ``needle``, asserting it exists at all.

    An ordering assertion whose anchor has vanished would pass vacuously, so a
    missing anchor is a failure here, never a skip.
    """
    body = _script()
    found = body.find(needle)
    if found == -1:
        raise AssertionError(
            f"{WORKFLOW.name}: expected the merge step's script to contain "
            f"{needle!r}, and it does not."
        )
    return found


def _line(contains: str) -> str:
    """The first line of the REAL script containing ``contains``, verbatim."""
    for line in _script().splitlines():
        if contains in line:
            return line.strip()
    raise AssertionError(
        f"{WORKFLOW.name}: no line contains {contains!r}. The hold logic these "
        "tests execute has moved or been renamed -- repair the anchor rather "
        "than deleting the test, or the sweep goes unguarded."
    )


def _block(opens_with: str, closer: str) -> str:
    """A shell block lifted verbatim from the REAL workflow.

    Starts at the first line containing ``opens_with``, ends at the first
    following line whose stripped content equals ``closer``, and is dedented so
    it can be embedded in the harness below.
    """
    lines = _script().splitlines()
    start = next((i for i, line in enumerate(lines) if opens_with in line), None)
    if start is None:
        raise AssertionError(
            f"{WORKFLOW.name}: no line contains {opens_with!r} -- the block "
            "these tests execute has moved or been renamed."
        )
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == closer), None
    )
    if end is None:
        raise AssertionError(
            f"{WORKFLOW.name}: block opening at line {start + 1} "
            f"({opens_with!r}) never closes with {closer!r}."
        )
    return textwrap.dedent("\n".join(lines[start : end + 1]))


def _decide(labels: str, draft: str = "false") -> str:
    """Run the SHIPPED hold logic under bash and return everything it printed.

    ``labels`` is the space-separated lower-cased string the workflow's
    ``gh pr view --json labels`` produces; ``draft`` is its ``isDraft`` string.
    Everything between those inputs and the verdict is the workflow's own code.

    The blocks are wrapped in a one-iteration ``for`` loop because the shipped
    hold branch ends in ``continue`` -- which IS the behaviour under test. If it
    fires, ``REACHED_MERGE`` is never printed.
    """
    harness = "\n".join(
        [
            "set -uo pipefail",
            'HOLD_LABELS="hold do-not-merge"',
            f"pr={PR_UNDER_TEST}",
            f'draft="{draft}"',
            f'pr_labels="{labels}"',
            "for _once in 1; do",
            '  hold=""',
            _line('[ "$draft" = "true" ]'),
            _block("for want in $HOLD_LABELS", "done"),
            _block('if [ -n "$hold" ]', "fi"),
            '  echo "REACHED_MERGE"',
            "done",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"the extracted hold logic failed to run: rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is needed to run the shipped shell"
)

HOLD_CASES = [
    ("hold", "false", "label:hold"),
    ("bug hold", "false", "label:hold"),
    ("do-not-merge", "false", "label:do-not-merge"),
    ("enhancement do-not-merge ci", "false", "label:do-not-merge"),
    ("enhancement", "true", "draft"),
]
HOLD_IDS = ["hold", "hold-among-others", "do-not-merge", "dnm-among-others", "draft"]


# ---------------------------------------------------------------------------
# Guard the guard.
# ---------------------------------------------------------------------------


def test_the_merge_step_is_findable():
    """If the merge moved, every test below would pass without checking it."""
    # Arrange
    workflow = WORKFLOW

    # Act
    script = _script()

    # Assert
    assert script.strip(), (
        f"{workflow} has no merge step carrying a {MERGE_CALL!r} call -- every "
        "test in this file would be vacuous."
    )


def test_hold_labels_are_declared_and_offer_hold():
    """The marker must be nameable, and `hold` is the one a human reaches for."""
    # Arrange
    env = _env()

    # Act
    declared = str(env.get("HOLD_LABELS", "")).split()

    # Assert
    assert "hold" in declared, (
        f"{WORKFLOW.name}: HOLD_LABELS is {env.get('HOLD_LABELS')!r}. It must "
        "offer at least `hold`. This list is a UI whose user is the operator "
        "clicking a label while a robot is about to merge something it should "
        "not, and a marker he cannot guess is no marker at all."
    )


# ---------------------------------------------------------------------------
# A. The hold is honoured -- by executing the shipped decision.
# ---------------------------------------------------------------------------


@requires_bash
@pytest.mark.parametrize("labels, draft, marker", HOLD_CASES, ids=HOLD_IDS)
def test_a_held_pull_request_never_reaches_the_merge(labels, draft, marker):
    """THE PROPERTY THE INCIDENT NEEDED."""
    # Arrange
    held_by = marker

    # Act
    printed = _decide(labels, draft)

    # Assert
    assert "REACHED_MERGE" not in printed, (
        f"a PR held by {held_by!r} (labels={labels!r}, draft={draft}) fell "
        "through to the merge path. This is the 2026-08-12 defect exactly: a "
        f"hold the automation does not honour.\nshipped logic printed:\n{printed}"
    )


@requires_bash
@pytest.mark.parametrize("labels, draft, marker", HOLD_CASES, ids=HOLD_IDS)
def test_the_refusal_is_a_github_annotation(labels, draft, marker):
    """Loud means it survives into the run summary, not just the raw log."""
    # Arrange
    held_by = marker

    # Act
    printed = _decide(labels, draft)

    # Assert
    assert "::warning::" in printed, (
        f"the refusal for a PR held by {held_by!r} is not a GitHub annotation, "
        "so it does not surface in the run summary. A silent skip reproduces "
        "the original bug in a new place: the hold works, nobody can tell that "
        f"it worked, and the next reader assumes it did not.\ngot:\n{printed}"
    )


@requires_bash
@pytest.mark.parametrize("labels, draft, marker", HOLD_CASES, ids=HOLD_IDS)
def test_the_refusal_names_the_pull_request(labels, draft, marker):
    """A refusal that does not say WHICH PR cannot be acted on."""
    # Arrange
    expected = f"#{PR_UNDER_TEST}"

    # Act
    printed = _decide(labels, draft)

    # Assert
    assert expected in printed, (
        f"the refusal does not name the PR ({expected}), so a reader watching "
        f"the sweep cannot tell what was held.\ngot:\n{printed}"
    )


@requires_bash
@pytest.mark.parametrize("labels, draft, marker", HOLD_CASES, ids=HOLD_IDS)
def test_the_refusal_names_the_marker(labels, draft, marker):
    """And WHICH marker, because that is the thing to remove."""
    # Arrange
    expected = marker

    # Act
    printed = _decide(labels, draft)

    # Assert
    assert expected in printed, (
        f"the refusal does not name the marker {expected!r}, so a reader cannot "
        f"tell why the PR was held or what to remove.\ngot:\n{printed}"
    )


@requires_bash
def test_every_marker_is_reported_not_just_the_first():
    """A PR that is both draft and labelled must report both, or removing one
    looks as though it should have been enough."""
    # Arrange
    both = ("draft", "label:hold")

    # Act
    printed = _decide("hold", draft="true")

    # Assert
    assert all(marker in printed for marker in both), (
        f"a PR held by BOTH draft status and a label reported only one of "
        f"{both}:\n{printed}"
    )


def test_the_label_comparison_is_case_folded():
    """GitHub keeps the case a label was created with. A hold must not leak
    through because somebody typed `Hold`."""
    # Arrange
    script = _script()

    # Act
    reads_labels_case_folded = "ascii_downcase" in script and "--json labels" in script

    # Assert
    assert reads_labels_case_folded, (
        f"{WORKFLOW.name}: the label read no longer lower-cases label names, so "
        "a `Hold` or `DO-NOT-MERGE` label would sail straight past the "
        "comparison against HOLD_LABELS."
    )


@requires_bash
def test_an_unheld_pull_request_still_reaches_the_merge():
    """The other half of the property. A hold that blocks everything is not a
    hold, it is an outage -- and removing the marker must need no other action."""
    # Arrange
    ordinary_labels = "enhancement bug"

    # Act
    printed = _decide(ordinary_labels)

    # Assert
    assert "REACHED_MERGE" in printed, (
        f"a non-draft PR labelled {ordinary_labels!r} was refused. Removing a "
        "hold marker has to be sufficient on its own to let the PR merge "
        f"again.\ngot:\n{printed}"
    )


@requires_bash
def test_an_unheld_pull_request_produces_no_hold_warning():
    """Noise trains readers to ignore the annotation that matters."""
    # Arrange
    ordinary_labels = "enhancement bug"

    # Act
    printed = _decide(ordinary_labels)

    # Assert
    assert "::warning::" not in printed, (
        f"an unheld PR produced a hold warning:\n{printed}"
    )


def test_the_hold_is_checked_before_the_merge():
    """A gate that runs after the merge is not a gate."""
    # Arrange
    hold_at = _at('if [ -n "$hold" ]')

    # Act
    merge_at = _at(MERGE_CALL)

    # Assert
    assert hold_at < merge_at, (
        f"{WORKFLOW.name}: the hold check appears AFTER the merge call, so it "
        "cannot prevent anything."
    )


def test_a_hold_costs_no_merge_budget():
    """MAX_MERGES is 1 per run. If a held PR spent that budget, holding one PR
    would silently stall every other PR behind it."""
    # Arrange
    hold_at = _at('if [ -n "$hold" ]')

    # Act
    budget_at = _at("merges=$((merges + 1))")

    # Assert
    assert hold_at < budget_at, (
        f"{WORKFLOW.name}: the hold is evaluated after the merge budget is "
        "spent, so one held PR delays the whole drain."
    )


def test_the_hold_is_checked_before_ci_state():
    """So that "why didn't this merge" has one answer and not two: a held PR
    must report as held even while it is red."""
    # Arrange
    hold_at = _at('if [ -n "$hold" ]')

    # Act
    ci_at = _at("statusCheckRollup")

    # Assert
    assert hold_at < ci_at, (
        f"{WORKFLOW.name}: hold markers are evaluated after CI state, so a red "
        "held PR reports only 'not green' and the hold looks broken."
    )


def test_a_hold_does_not_comment_on_the_pull_request():
    """Stable, not accumulating. The sweep ticks every ~15 minutes; a hold that
    commented on each tick would bury the PR under its own enforcement."""
    # Arrange
    script = _script()
    hold_at = _at('if [ -n "$hold" ]')

    # Act
    hold_branch = script[hold_at : script.find("continue", hold_at)]

    # Assert
    assert "gh pr comment" not in hold_branch, (
        f"{WORKFLOW.name}: the hold branch posts a PR comment. The refusal "
        "belongs in the run log, which is per-run and ephemeral; on a "
        "15-minute tick a commenting hold would add ~96 comments a day."
    )


# ---------------------------------------------------------------------------
# B. An automated merge announces itself.
# ---------------------------------------------------------------------------


def test_the_attribution_comment_is_posted_before_the_merge():
    """Before, not after, so a merge can never exist without its explanation."""
    # Arrange
    comment_at = _at("gh pr comment")

    # Act
    merge_at = _at(MERGE_CALL)

    # Assert
    assert comment_at < merge_at, (
        f"{WORKFLOW.name}: the attribution comment is posted AFTER the merge. "
        "If it then fails, the merge has already happened and is once again "
        "indistinguishable from a human's."
    )


def test_the_attribution_names_the_automation():
    """Someone reading this PR in six months is the audience."""
    # Arrange
    body = _script()[: _at("gh pr comment")]

    # Act
    names_itself = "auto-merge-to-develop" in body

    # Assert
    assert names_itself, (
        f"{WORKFLOW.name}: the attribution comment does not name the automation "
        "that merged the PR, so a reader cannot find the thing that acted."
    )


def test_the_attribution_explains_why_merged_by_cannot_be_trusted():
    """Without that, a reader assumes `merged_by` is meaningful and concludes
    the comment is redundant."""
    # Arrange
    body = _script()[: _at("gh pr comment")].lower()

    # Act
    explains_shared_identity = "merged_by" in body

    # Assert
    assert explains_shared_identity, (
        f"{WORKFLOW.name}: the attribution comment never mentions `merged_by`, "
        "so it does not explain why it exists -- that the merging identity is "
        "shared across the whole fleet and therefore says nothing."
    )


def test_the_attribution_says_how_to_stop_the_next_automated_merge():
    """The comment is the one place a surprised human is guaranteed to look."""
    # Arrange
    body = _script()[: _at("gh pr comment")].lower()

    # Act
    documents_the_hold = "hold" in body

    # Assert
    assert documents_the_hold, (
        f"{WORKFLOW.name}: the attribution comment does not tell the reader how "
        "to stop the next automated merge, which is the first thing somebody "
        "surprised by this one will want to know."
    )


def test_a_merge_without_attribution_does_not_happen():
    """The load-bearing failure mode. Merging anyway would trade a 15-minute
    delay for an untraceable merge."""
    # Arrange
    script = _script()

    # Act
    between = script[_at("gh pr comment") : _at(MERGE_CALL)]

    # Assert
    assert "continue" in between, (
        f"{WORKFLOW.name}: nothing between the attribution comment and the "
        "merge abandons the PR, so a failed comment still lets the merge "
        "through -- exactly the untraceable merge the comment exists to stop."
    )


def test_a_failed_attribution_is_counted():
    """A PR passed over in silence is how a stalled drain hides."""
    # Arrange
    script = _script()

    # Act
    between = script[_at("gh pr comment") : _at(MERGE_CALL)]

    # Assert
    assert "attrib_failures" in between, (
        f"{WORKFLOW.name}: a failed attribution is not counted, so the run "
        "stays green while a green PR was silently skipped."
    )


def test_an_attribution_failure_turns_the_run_red():
    """A stalled drain must be visible -- the whole lesson of this workflow."""
    # Arrange
    tail = _script()[_at("sweep complete") :]

    # Act
    fails_the_run = "attrib_failures" in tail and "rc=1" in tail

    # Assert
    assert fails_the_run, (
        f"{WORKFLOW.name}: attribution failures never reach the run's exit "
        "status, so a sweep that merged nothing for that reason looks exactly "
        "like a quiet, healthy tick."
    )


def test_an_attribution_marker_is_declared():
    """Without a signature the workflow cannot recognise its own comment."""
    # Arrange
    env = _env()

    # Act
    marker = str(env.get("ATTRIB_MARKER", ""))

    # Assert
    assert marker.strip(), (
        f"{WORKFLOW.name}: no ATTRIB_MARKER is declared, so a merge refused by "
        "branch policy would be re-attributed on every subsequent tick."
    )


def test_the_attribution_marker_both_signs_and_matches():
    """A declared-but-unused marker is worse than none: it reads as solved.

    The marker has to be referenced TWICE -- once to sign the comment being
    posted, once to recognise a comment posted by an earlier tick. One without
    the other is half a mechanism: signing nothing means the lookup never
    matches, and matching nothing means every tick re-attributes.
    """
    # Arrange
    script = _script()

    # Act
    references = script.count("ATTRIB_MARKER")

    # Assert
    assert references >= 2, (
        f"{WORKFLOW.name}: ATTRIB_MARKER is referenced {references} time(s) in "
        "the merge script. It must both sign the comment body and be looked "
        "for in the existing comments."
    )


def test_existing_comments_are_read_before_attributing():
    """The lookup half of at-most-once."""
    # Arrange
    script = _script()

    # Act
    reads_comments = "--json comments" in script

    # Assert
    assert reads_comments, (
        f"{WORKFLOW.name}: the workflow never reads existing comments, so it "
        "cannot tell whether it has already attributed this PR and will repeat "
        "itself on every tick that fails to merge."
    )


def test_a_dry_run_neither_comments_nor_merges():
    """`workflow_dispatch` with dry_run:true is how a human inspects the sweep.
    It has to stay side-effect free now that the sweep writes to PRs."""
    # Arrange
    dry_at = _at('if [ "$DRY_RUN" = "true" ]')

    # Act
    comment_at = _at("gh pr comment")

    # Assert
    assert dry_at < comment_at, (
        f"{WORKFLOW.name}: the attribution comment is reached before the "
        "DRY_RUN branch is taken, so a dry run now writes a comment to every "
        "candidate PR."
    )
