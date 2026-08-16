"""auto-merge-to-develop.yaml must dispatch develop's post-merge gates.

A github.token merge push triggers no workflows, so the merge step fires
POST_MERGE_GATES explicitly (once per tick, --ref develop, loud on failure).
File-only assertions on the workflow YAML — no network, cannot flake.
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
    "lint.yml",
    "import-smoke-on-ubuntu-py3-12.yml",
    "no-hosted-runners-guard-on-self-hosted.yml",
)

# `quality-audit-on-ubuntu-latest.yml` USED TO BE IN THIS LIST and was removed
# with the workflow itself. It is named here, not silently dropped, because a
# shrinking required-set is exactly the drift the tests below exist to catch:
# it dispatched a job whose five audit steps all exited 2 on the installed
# scitex-dev (pre-0.11 `quality audit-*` spellings) under
# `continue-on-error: true`, so the gate was permanently green while auditing
# nothing. The real audit gate is `tests/develop/test_audit.py`, which runs
# `scitex-dev ecosystem audit-all` inside the pytest matrix leg already listed
# above — so dispatching the deleted workflow added no coverage.


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
