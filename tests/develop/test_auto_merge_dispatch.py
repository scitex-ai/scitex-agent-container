"""The auto-merge sweep must DISPATCH develop's post-merge gates itself.

GitHub SUPPRESSES workflow triggers for pushes made with the default
``github.token`` (recursive-run protection), and auto-merge-to-develop.yaml
merges with exactly that token — so every commit the sweep lands arrives on
develop with ZERO check runs. Measured on this repo: bot-merged develop head
``ae09a078`` -> ``check-runs total_count: 0``, while its user-pushed
neighbours carried 9 check runs each, with runners online and idle. The
sweep's own develop-health gate then reads "no checks" as "no red signal to
honour" and keeps merging — green-by-absence, the exact
``reference-evidence-that-could-not-have-disagreed`` shape the file's header
warns about.

``workflow_dispatch`` IS exempt from that suppression (the escape hatch
autobump-release-sweep.yaml already documents for tags), so the merge step
must explicitly dispatch every develop gate after a successful merge. These
tests pin that wiring AS TEXT — no network, no gh, cannot flake:

* the merge step names every required gate (``POST_MERGE_GATES``);
* it dispatches with ``gh workflow run ... --ref develop``;
* the dispatch is reachable only AFTER a real merge (guarded on the merge
  count, skipped on dry-run) — N merged PRs must produce ONE dispatch fan-out,
  not N;
* the workflow token is actually allowed to dispatch (``actions: write``);
* every dispatched gate still accepts a bare ``workflow_dispatch`` — a gate
  that drops the trigger would turn the dispatch into a silent 404, quietly
  rebuilding the un-CI'd-develop hole this wiring exists to close.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_AUTO_MERGE = _WORKFLOWS / "auto-merge-to-develop.yaml"

# The develop-facing gates. Keep in lockstep with POST_MERGE_GATES in
# auto-merge-to-develop.yaml — test_gate_list_is_exactly_the_required_set
# fails on any drift in either direction.
REQUIRED_GATES = (
    "pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml",
    "quality-audit-on-ubuntu-latest.yml",
    "lint.yml",
    "import-smoke-on-ubuntu-py3-12.yml",
    "no-hosted-runners-guard-on-self-hosted.yml",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_AUTO_MERGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def merge_step(workflow: dict) -> dict:
    """The step that performs the merges (found by behaviour, not by name)."""
    steps = workflow["jobs"]["automerge"]["steps"]
    merging = [s for s in steps if "gh pr merge" in s.get("run", "")]
    assert len(merging) == 1, "expected exactly one merging step"
    return merging[0]


def _gates_env(merge_step: dict) -> str:
    return merge_step.get("env", {}).get("POST_MERGE_GATES", "")


def _dispatch_lines(merge_step: dict) -> list[str]:
    return [
        line for line in merge_step["run"].splitlines() if "gh workflow run" in line
    ]


# ---------------------------------------------------------------------------
# The dispatch exists and covers every gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate", REQUIRED_GATES)
def test_every_required_gate_is_dispatched(merge_step: dict, gate: str) -> None:
    # Arrange
    declared = _gates_env(merge_step)
    # Act
    gates = declared.split()
    # Assert
    assert gate in gates, (
        f"{gate} is not in POST_MERGE_GATES — a github.token merge fires no "
        "triggers, so an undispatched gate simply never runs on develop"
    )


def test_gate_list_is_exactly_the_required_set(merge_step: dict) -> None:
    # Arrange
    declared = _gates_env(merge_step)
    # Act: drift in EITHER direction is a finding — a missing gate is a hole,
    # a surplus one is an undocumented dependency of the sweep.
    gates = declared.split()
    # Assert
    assert sorted(gates) == sorted(REQUIRED_GATES)


def test_merge_step_runs_workflow_dispatch(merge_step: dict) -> None:
    # Arrange
    run = merge_step["run"]
    # Act
    lines = [line for line in run.splitlines() if "gh workflow run" in line]
    # Assert
    assert lines, "the merge step never runs `gh workflow run`"


def test_every_dispatch_targets_develop(merge_step: dict) -> None:
    # Arrange: non-emptiness is pinned by test_merge_step_runs_workflow_dispatch.
    lines = _dispatch_lines(merge_step)
    # Act
    off_ref = [line for line in lines if "--ref develop" not in line]
    # Assert
    assert off_ref == [], (
        "a dispatch without --ref develop runs the gate from the DEFAULT "
        "branch ref, not against the head the merge just produced"
    )


# ---------------------------------------------------------------------------
# Reachable only AFTER a merge — once per tick, never per PR, never dry.
# ---------------------------------------------------------------------------


def test_dispatch_is_guarded_by_the_merge_count(merge_step: dict) -> None:
    # Arrange
    run = merge_step["run"]
    # Act: locate the end of the per-PR merge loop (the first column-0 `done`
    # after `gh pr merge`), the merge-count guard, and the dispatch.
    loop_end_at = run.index("\ndone\n", run.index("gh pr merge"))
    guard_at = run.index('[ "$merges" -gt 0 ]')
    dispatch_at = run.index("gh workflow run")
    # Assert: the guard opens after the merge loop and before the dispatch,
    # so N merges fan out to ONE dispatch round, not N.
    assert loop_end_at < guard_at < dispatch_at, (
        "the dispatch must be guarded on `merges > 0` after the merge loop — "
        "a per-PR or unconditional dispatch is the wrong shape"
    )


def test_dry_run_does_not_dispatch(merge_step: dict) -> None:
    # Arrange
    run = merge_step["run"]
    # Act: the dry-run branch must be decided between the guard and the
    # dispatch — a dry run merges nothing, so firing real CI from it would
    # be dispatching gates for a merge that never happened.
    guarded_block = run[run.index('[ "$merges" -gt 0 ]') : run.index("gh workflow run")]
    # Assert
    assert '"$DRY_RUN" = "true"' in guarded_block


def test_dispatch_failure_is_loud(merge_step: dict) -> None:
    # Arrange
    run = merge_step["run"]
    # Act
    after_guard = run[run.index('[ "$merges" -gt 0 ]') :]
    # Assert: a failed dispatch leaves develop's new head UN-CHECKED — the
    # precise silence this wiring exists to end — so it must go ::error::
    # red, not vanish into an `|| true`.
    assert "::error::" in after_guard, (
        "a failed gate dispatch must be loud (::error:: + red run), "
        "not silently swallowed"
    )


# ---------------------------------------------------------------------------
# The token and the targets can actually honour the dispatch.
# ---------------------------------------------------------------------------


def test_workflow_token_may_dispatch(workflow: dict) -> None:
    # Arrange
    permissions = workflow.get("permissions", {})
    # Act: `gh workflow run` needs actions:write; without it every
    # dispatch 403s and the sweep is back to landing un-CI'd commits.
    granted = permissions.get("actions")
    # Assert
    assert granted == "write"


@pytest.mark.parametrize("gate", REQUIRED_GATES)
def test_dispatched_gate_accepts_workflow_dispatch(gate: str) -> None:
    # Arrange
    doc = yaml.safe_load((_WORKFLOWS / gate).read_text(encoding="utf-8"))
    # Act: YAML 1.1 parses a bare `on:` key as boolean True — read both.
    triggers = doc.get("on", doc.get(True, {}))
    # Assert
    assert "workflow_dispatch" in triggers, (
        f"{gate} no longer accepts workflow_dispatch — dispatching it "
        "would 404 and develop's head would sit un-checked again"
    )
