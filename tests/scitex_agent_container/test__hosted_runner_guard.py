"""Tests for the no-GitHub-hosted-runner guard.

A GUARD YOU HAVE ONLY EVER SEEN PASS IS A HOPE. We merged a "fail-loud"
version gate earlier this week that returned exit 0 on the exact broken
artifact it existed to reject — because it was calibrated to the last
incident and nobody ever ran it against a bad input.

So this file proves BOTH directions:

* the guard FAILS (non-zero, with the right code) on a workflow that lands
  on a hosted runner — one test per evasion we could think of;
* the guard PASSES on the real ``.github/workflows/`` of this repo — the
  11 migrated self-hosted jobs plus the one allowlisted exception.

``test_this_repo_is_clean`` is also the false-positive alarm: if the guard
ever red-flags the migrated jobs, or stops honouring the cla.yml
exception, this file goes red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._hosted_runner_guard import (
    CODE_ALLOWLIST_NO_REASON,
    CODE_ALLOWLIST_STALE,
    CODE_HOSTED,
    CODE_UNRESOLVABLE,
    HOSTED,
    SELF_HOSTED,
    UNRESOLVABLE,
    check_repo,
    classify_runs_on,
    load_allowlist,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The canonical self-hosted target our 11 migrated jobs actually use.
CANONICAL = (
    '${{ fromJSON(vars.CI_RUNS_ON || \'["self-hosted","Linux","X64","scitex-ci"]\') }}'
)

GOOD_REASON = (
    "This job may run on GitHub's hardware because its triggers are "
    "unauthenticated and it runs a third-party action, which on the "
    "self-hosted node would reach the operator's live credentials."
)

MATRIX_FANOUT = (
    "name: demo\n"
    "on: [push]\n"
    "jobs:\n"
    "  test:\n"
    "    strategy:\n"
    "      matrix:\n"
    "        os: [ubuntu-latest, macos-latest]\n"
    "    runs-on: ${{ matrix.os }}\n"
    "    steps:\n"
    "      - run: echo hi\n"
)

TWO_HOSTED_JOBS = (
    "name: cla\n"
    "on: [push]\n"
    "jobs:\n"
    "  CLAssistant:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: echo hi\n"
    "  sneaky:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: echo hi\n"
)

REUSABLE_CALLER = (
    "name: demo\non: [push]\njobs:\n  call:\n    uses: ./.github/workflows/other.yml\n"
)

# The shape PS-231 pushes a leaf into: the body is replaced by a call to the
# org reusable, so the runner is decided by the CALLEE's input default — in
# another repository, which this guard cannot read.
ORG_CALLER = (
    "name: cla\n"
    "on: [issue_comment]\n"
    "jobs:\n"
    "  CLAssistant:\n"
    "    uses: scitex-ai/.github/.github/workflows/cla.yml@main\n"
    "    with:\n"
    "      runs_on: '[\"ubuntu-latest\"]'\n"
)

LEGACY_NAMED = (
    "name: quality\n"
    "on: [push]\n"
    "jobs:\n"
    "  audit:\n"
    "    name: scitex-dev-quality-audit-on-ubuntu-latest\n"
    f"    runs-on: {CANONICAL}\n"
    "    steps:\n"
    "      - run: echo hi\n"
)


def _workflow(runs_on: str, job_id: str = "build") -> str:
    return (
        "name: demo\n"
        "on: [push]\n"
        "jobs:\n"
        f"  {job_id}:\n"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )


def _write_repo(
    root: Path,
    workflows: dict[str, str],
    allowlist: str | None = None,
) -> Path:
    """Materialise a throwaway repo with real files on disk (no mocks)."""
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    for name, body in workflows.items():
        (wf_dir / name).write_text(body, encoding="utf-8")
    if allowlist is not None:
        (root / ".github" / "hosted-runner-allowlist.yaml").write_text(
            allowlist, encoding="utf-8"
        )
    return root


def _allowlist(workflow: str, jobs: str = "", reason: str = GOOD_REASON) -> str:
    jobs_line = f"    jobs: [{jobs}]\n" if jobs else ""
    return (
        f"allow:\n  - workflow: {workflow}\n{jobs_line}    reason: >-\n      {reason}\n"
    )


def _codes(root: Path) -> list[str]:
    return [violation.code for violation in check_repo(root)]


# ───────────────────────────────────────────────────────────────────────────
# FAIL DIRECTION — the guard must actually fire.
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        "ubuntu-latest",  # the literal everyone greps for
        "ubuntu-24.04",  # ...and the one they write next week
        "ubuntu-22.04",
        "ubuntu-latest-4-cores",  # larger runners
        "macos-latest",
        "macos-14",
        "windows-latest",
        "windows-2022",
    ],
)
def test_every_hosted_image_family_is_flagged(tmp_path: Path, label: str) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(label)})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_HOSTED]


def test_hosted_runner_exits_non_zero(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow("ubuntu-latest")})
    # Act
    exit_code = main([str(tmp_path)])
    # Assert
    assert exit_code == 1


def test_hosted_runner_message_names_the_offence(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow("ubuntu-latest")})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert "GitHub-HOSTED runner" in violations[0].message


def test_hosted_runner_in_list_form_is_flagged(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow("[ubuntu-latest]")})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_HOSTED]


def test_hosted_matrix_fanout_is_flagged(tmp_path: Path) -> None:
    # Arrange: `runs-on: ${{ matrix.os }}` resolved against the matrix values
    _write_repo(tmp_path, {"ci.yml": MATRIX_FANOUT})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_HOSTED]


def test_hosted_default_smuggled_into_fromjson_is_flagged(tmp_path: Path) -> None:
    # Arrange
    runs_on = "${{ fromJSON(vars.CI_RUNS_ON || '[\"ubuntu-latest\"]') }}"
    _write_repo(tmp_path, {"ci.yml": _workflow(runs_on)})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_HOSTED]


def test_unresolvable_runs_on_is_refused(tmp_path: Path) -> None:
    # Arrange: we cannot PROVE `${{ vars.WHATEVER }}` is ours. Absence of
    # evidence is not evidence of self-hosting, and an unreadable runner
    # target is exactly the shape a deliberate bypass would take.
    _write_repo(tmp_path, {"ci.yml": _workflow("${{ vars.WHATEVER }}")})
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_UNRESOLVABLE]


def test_allowlist_entry_without_reason_is_rejected(tmp_path: Path) -> None:
    # Arrange: the mandatory `reason:` is enforced by the MECHANISM
    _write_repo(
        tmp_path,
        {"cla.yml": _workflow("ubuntu-latest", job_id="CLAssistant")},
        allowlist="allow:\n  - workflow: cla.yml\n",
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_NO_REASON in codes


def test_allowlist_entry_without_reason_does_not_exempt_the_job(
    tmp_path: Path,
) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"cla.yml": _workflow("ubuntu-latest", job_id="CLAssistant")},
        allowlist="allow:\n  - workflow: cla.yml\n",
    )
    # Act: a rejected entry must not smuggle the job through
    codes = _codes(tmp_path)
    # Assert
    assert CODE_HOSTED in codes


def test_allowlist_entry_with_stub_reason_is_rejected(tmp_path: Path) -> None:
    # Arrange: a shrug ("legacy") is not an argument
    _write_repo(
        tmp_path,
        {"cla.yml": _workflow("ubuntu-latest", job_id="CLAssistant")},
        allowlist='allow:\n  - workflow: cla.yml\n    reason: "legacy"\n',
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_NO_REASON in codes


def test_stale_allowlist_entry_is_flagged(tmp_path: Path) -> None:
    # Arrange: ci.yml is self-hosted, so it needs no exception. A dead
    # exception is a live loophole — it pre-approves the next hosted job.
    _write_repo(
        tmp_path,
        {"ci.yml": _workflow(CANONICAL)},
        allowlist=_allowlist("ci.yml"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_ALLOWLIST_STALE]


def test_allowlist_entry_for_missing_workflow_is_flagged(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"ci.yml": _workflow(CANONICAL)},
        allowlist=_allowlist("gone.yml"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_ALLOWLIST_STALE]


def test_allowlisted_caller_is_not_reported_stale(tmp_path: Path) -> None:
    # Arrange: cla.yml has become a CALLER (PS-231). Its runner is now decided
    # by the callee's input default, in another repository this guard cannot
    # read. Reporting the entry stale here would tell the reader to delete the
    # SECURITY ARGUMENT for a workflow whose triggers are unauthenticated and
    # which runs a third-party action — the one thing standing in front of a
    # one-line change to a self-hosted pool. Unresolvable is not unhosted.
    _write_repo(
        tmp_path,
        {"cla.yml": ORG_CALLER},
        allowlist=_allowlist("cla.yml"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_STALE not in codes


def test_caller_outside_the_job_scope_leaves_the_entry_stale(
    tmp_path: Path,
) -> None:
    # Arrange: the OTHER direction, and the one that keeps the fix honest. The
    # entry is scoped to job `CLAssistant`, but the file's only caller is named
    # `unrelated`. Nothing the entry covers is present, so the entry really IS
    # stale and must still be reported. A fix that marked ANY caller as "in
    # use" would silently keep every job-scoped entry alive for ever, which is
    # the standing-loophole this rule exists to prevent.
    caller_named_otherwise = ORG_CALLER.replace("CLAssistant:", "unrelated:")
    _write_repo(
        tmp_path,
        {"cla.yml": caller_named_otherwise},
        allowlist=_allowlist("cla.yml", jobs="CLAssistant"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert CODE_ALLOWLIST_STALE in codes


def test_job_scoped_allowlist_does_not_cover_a_new_job(tmp_path: Path) -> None:
    # Arrange: entry covers CLAssistant only; `sneaky` must not ride along
    _write_repo(
        tmp_path,
        {"cla.yml": TWO_HOSTED_JOBS},
        allowlist=_allowlist("cla.yml", jobs="CLAssistant"),
    )
    # Act
    codes = _codes(tmp_path)
    # Assert
    assert codes == [CODE_HOSTED]


def test_job_scoped_allowlist_names_the_uncovered_job(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"cla.yml": TWO_HOSTED_JOBS},
        allowlist=_allowlist("cla.yml", jobs="CLAssistant"),
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert "sneaky" in violations[0].where


# ───────────────────────────────────────────────────────────────────────────
# PASS DIRECTION — the guard must not cry wolf.
# ───────────────────────────────────────────────────────────────────────────


def test_canonical_self_hosted_expression_passes(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(CANONICAL)})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_canonical_self_hosted_expression_exits_zero(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow(CANONICAL)})
    # Act
    exit_code = main([str(tmp_path)])
    # Assert
    assert exit_code == 0


def test_plain_self_hosted_label_list_passes(tmp_path: Path) -> None:
    # Arrange
    _write_repo(tmp_path, {"ci.yml": _workflow("[self-hosted, Linux, X64]")})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_legacy_ubuntu_filenames_and_job_names_are_not_flagged(
    tmp_path: Path,
) -> None:
    # Arrange: the naive-grep trap. Both the FILENAME and the job `name:`
    # still say ubuntu-latest, but only `runs-on:` decides where it runs.
    _write_repo(tmp_path, {"quality-audit-on-ubuntu-latest.yml": LEGACY_NAMED})
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_reusable_workflow_call_is_not_flagged(tmp_path: Path) -> None:
    # Arrange: a `uses:` job declares no runner of its own
    _write_repo(
        tmp_path,
        {
            "caller.yml": REUSABLE_CALLER,
            "other.yml": _workflow(CANONICAL, job_id="inner"),
        },
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_allowlisted_hosted_job_passes(tmp_path: Path) -> None:
    # Arrange: the approved exception, with its argument attached
    _write_repo(
        tmp_path,
        {"cla.yml": _workflow("ubuntu-latest", job_id="CLAssistant")},
        allowlist=_allowlist("cla.yml", jobs="CLAssistant"),
    )
    # Act
    violations = check_repo(tmp_path)
    # Assert
    assert violations == []


def test_allowlisted_hosted_job_exits_zero(tmp_path: Path) -> None:
    # Arrange
    _write_repo(
        tmp_path,
        {"cla.yml": _workflow("ubuntu-latest", job_id="CLAssistant")},
        allowlist=_allowlist("cla.yml", jobs="CLAssistant"),
    )
    # Act
    exit_code = main([str(tmp_path)])
    # Assert
    assert exit_code == 0


# ───────────────────────────────────────────────────────────────────────────
# The verdict is a TERNARY, never a boolean.
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "runs_on,expected",
    [
        ("ubuntu-latest", HOSTED),
        ("self-hosted", SELF_HOSTED),
        (["self-hosted", "Linux"], SELF_HOSTED),
        # `self-hosted` is positive proof — it is OUR box even if a sibling
        # label happens to name an OS image.
        (["self-hosted", "ubuntu-latest"], SELF_HOSTED),
        ("${{ vars.MYSTERY }}", UNRESOLVABLE),
        (None, UNRESOLVABLE),
    ],
)
def test_classify_runs_on_returns_the_right_verdict(runs_on, expected: str) -> None:
    # Arrange
    job = {"runs-on": runs_on}
    # Act
    verdict = classify_runs_on(job)
    # Assert
    assert verdict == expected


# ───────────────────────────────────────────────────────────────────────────
# THE REAL REPO — the trap the brief warned about.
# ───────────────────────────────────────────────────────────────────────────


def test_this_repo_is_clean() -> None:
    # Arrange: sac's own workflows — 11 migrated jobs + 1 allowlisted
    repo = REPO_ROOT
    # Act
    violations = check_repo(repo)
    # Assert
    assert violations == [], "\n".join(v.render() for v in violations)


def test_the_cla_allowlist_entry_is_present() -> None:
    # Arrange
    repo = REPO_ROOT
    # Act
    allow, _ = load_allowlist(repo)
    # Assert
    assert "cla.yml" in allow, "the operator-approved cla.yml exception vanished"


def test_the_cla_allowlist_entry_carries_a_real_reason() -> None:
    # Arrange
    repo = REPO_ROOT
    # Act
    _, allow_violations = load_allowlist(repo)
    # Assert
    assert allow_violations == []


def test_the_cla_job_really_is_still_hosted() -> None:
    # Arrange: the exception must be LOAD-BEARING, not decorative — if cla.yml
    # ever stops being hosted, the entry is a standing loophole and must go.
    #
    # This used to assert `classify_runs_on(...) == HOSTED`, reading a local
    # `runs-on:`. cla.yml is now a CALLER (PS-231), so there is no local
    # `runs-on` and that verdict is UNRESOLVABLE — which is a statement about
    # what the guard can SEE, not about where the job runs, and so cannot
    # carry this assertion any more.
    #
    # The caller passes `runs_on` EXPLICITLY, which is what makes the property
    # locally checkable again: we assert the value this repo actually sends,
    # rather than a remote default we would have to go and look up. If someone
    # points this at the self-hosted pool, this test goes red — which is the
    # whole point, because that is the change the allowlist entry exists to
    # prevent (unauthenticated triggers, third-party action, and a $HOME
    # holding the fleet credential).
    cla = REPO_ROOT / ".github" / "workflows" / "cla.yml"
    doc = yaml.safe_load(cla.read_text(encoding="utf-8"))
    # Act
    labels = json.loads(doc["jobs"]["CLAssistant"]["with"]["runs_on"])
    # Assert
    assert labels == ["ubuntu-latest"]
