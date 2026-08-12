"""A superseded PR run must be CANCELLED, and nothing else may be.

MEASURED on this repo 2026-08-11, over the last 276 push/pull_request runs:

    runs a newer push obsoleted while they were still in flight   86  (31.2%)
    total CI machine time                                       3591  min
    machine time spent on those already-stale runs              1841  min  (51.3%)

Half of every minute this repo's runners spent went to proving something about a
commit nobody would merge. In the same window, queueing was 72.2% of job latency
across only 2 runner slots -- so the dead work was not free, it was the thing the
live work was waiting behind. An agent pushing three fixups to one PR fired three
full gates and all three ran to completion.

``concurrency:`` fixes that, and it is also the one knob here that can BREAK the
release path, which is why these tests exist rather than a review comment. The
obvious spelling --

    group: ${{ github.workflow }}-${{ github.ref }}

-- puts every push to ``develop`` into ONE group. With ``cancel-in-progress``
false those pushes then SERIALISE (a group runs one at a time), and with it true
a push to ``develop`` CANCELS the in-flight run whose conclusion
``autobump-release-sweep.yaml`` and ``auto-merge-to-develop.yaml`` read. Either
way the fix for a PR-latency problem lands squarely on the release path.

The shipped expression keys NON-PR events on ``github.run_id``, which is unique
per run, so such a run is alone in its group and can neither cancel nor be
cancelled. ``test_non_pr_events_are_alone_in_their_group`` is the guard on that
property; it fails on exactly the spelling above.

No mocks: every assertion parses the real ``.github/workflows/*.y*ml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# cla.yml is the one PR-triggered workflow deliberately left alone: it is the
# documented exception in .github/hosted-runner-allowlist.yaml and runs on a
# GitHub-hosted runner, so it consumes none of the self-hosted capacity this
# change exists to reclaim. Adding it here would be harmless but dishonest --
# the rule is about our own runners.
EXEMPT = {"cla.yml"}

# The two workflows that already carry a DELIBERATE, non-cancelling group. They
# serialise repository-wide side effects (a version bump, an auto-merge) where
# two concurrent runs would race each other, and cancelling one mid-flight is
# exactly what must not happen. They are asserted to STAY non-cancelling.
DELIBERATELY_SERIAL = {
    "autobump-release-sweep.yaml",
    "auto-merge-to-develop.yaml",
}

PUBLISH_WORKFLOW = "pypi-publish-and-github-release-on-tag.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(doc: dict) -> dict:
    # PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1), so a
    # workflow's trigger block is reachable under `True`, not "on". Reading only
    # "on" silently returns {} for every workflow and makes every test below
    # vacuously pass.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {k: None for k in raw}
    return raw or {}


def _concurrency(path: Path) -> dict:
    value = _load(path).get("concurrency")
    return value if isinstance(value, dict) else {}


def _group(path: Path) -> str:
    return str(_concurrency(path).get("group", ""))


def _pr_gate_workflows() -> list[Path]:
    out = []
    for path in sorted(WORKFLOW_DIR.iterdir()):
        if path.suffix not in (".yml", ".yaml"):
            continue
        if path.name in EXEMPT or path.name in DELIBERATELY_SERIAL:
            continue
        if "pull_request" in _triggers(_load(path)):
            out.append(path)
    return out


PR_GATE = _pr_gate_workflows()


def test_pr_gate_workflow_set_is_not_empty():
    """Guard the guard: an empty set makes every test below vacuous."""
    # Arrange
    workflow_dir = WORKFLOW_DIR

    # Act
    found = _pr_gate_workflows()

    # Assert
    assert found, (
        f"no pull_request-triggered workflows found under {workflow_dir} -- "
        "either the trigger parsing broke or the gate moved; the tests below "
        "would all pass without checking anything"
    )


@pytest.mark.parametrize("path", PR_GATE, ids=lambda p: p.name)
def test_pr_gate_declares_a_concurrency_block(path: Path):
    # Arrange
    name = path.name

    # Act
    conc = _concurrency(path)

    # Assert
    assert conc, (
        f"{name} runs on pull_request but declares no `concurrency:` mapping. "
        "Every push to a PR then starts a full gate while the previous one "
        "keeps running -- 51.3% of this repo's measured CI machine time went "
        "to exactly that. See the module docstring for the expression to use."
    )


@pytest.mark.parametrize("path", PR_GATE, ids=lambda p: p.name)
def test_superseded_pr_runs_are_cancelled(path: Path):
    # Arrange
    name = path.name

    # Act
    cancel = _concurrency(path).get("cancel-in-progress")

    # Assert
    assert cancel in (True, "true"), (
        f"{name}: cancel-in-progress is {cancel!r}. A group without "
        "cancellation does not free the runner slot -- it QUEUES the new run "
        "behind the stale one, which is worse than no group at all."
    )


@pytest.mark.parametrize("path", PR_GATE, ids=lambda p: p.name)
def test_pr_runs_are_grouped_by_pull_request_number(path: Path):
    # Arrange
    name = path.name

    # Act
    group = _group(path)

    # Assert
    assert "github.event.pull_request.number" in group, (
        f"{name}: concurrency group is {group!r}. It must key PR events on the "
        "PR number so that a new push to that PR -- and only that PR -- "
        "cancels its own previous run."
    )


@pytest.mark.parametrize("path", PR_GATE, ids=lambda p: p.name)
def test_non_pr_events_are_alone_in_their_group(path: Path):
    """THE SAFETY PROPERTY. A push/schedule/dispatch run must key on something
    unique to itself, so ``cancel-in-progress: true`` can never reach it.

    Fails on ``${{ github.workflow }}-${{ github.ref }}``, the spelling that
    would serialise or cancel ``develop`` pushes.
    """
    # Arrange
    name = path.name

    # Act
    group = _group(path)

    # Assert
    assert "github.run_id" in group, (
        f"{name}: concurrency group is {group!r}. Non-pull_request events must "
        "fall back to github.run_id (unique per run). Falling back to "
        "github.ref puts every push to `develop` in ONE group, where this "
        "workflow would either serialise them or cancel the in-flight run that "
        "autobump-release-sweep.yaml and auto-merge-to-develop.yaml read."
    )


@pytest.mark.parametrize("name", sorted(DELIBERATELY_SERIAL), ids=lambda n: n)
def test_deliberately_serial_workflows_still_do_not_cancel(name: str):
    """These serialise repository-wide side effects. Cancelling one mid-flight
    is the failure mode, not the fix."""
    # Arrange
    path = WORKFLOW_DIR / name

    # Act
    cancel = _concurrency(path).get("cancel-in-progress")

    # Assert
    assert cancel in (False, "false"), (
        f"{name}: cancel-in-progress is {cancel!r}. This workflow mutates the "
        "repository (version bump / auto-merge); a cancelled run can leave "
        "that half-done."
    )


def test_tag_publish_workflow_has_no_shared_cancelling_group():
    """A release must not be cancellable by anything. A shared cancelling group
    would let a second tag push kill an in-flight publish partway through."""
    # Arrange
    conc = _concurrency(WORKFLOW_DIR / PUBLISH_WORKFLOW)

    # Act
    cancels_and_is_shared = (
        conc.get("cancel-in-progress") in (True, "true")
        and "github.run_id" not in str(conc.get("group", ""))
    )

    # Assert
    assert not cancels_and_is_shared, (
        f"{PUBLISH_WORKFLOW} declares a CANCELLING concurrency group "
        f"({conc.get('group')!r}) that is not unique per run -- a second tag "
        "push could cancel an in-flight publish."
    )
