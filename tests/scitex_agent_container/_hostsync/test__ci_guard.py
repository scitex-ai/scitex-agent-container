"""Tests for the CI-idle guard.

No mocks: the ``gh`` shell-out is exercised through real injected
``runner`` callables that answer like the real API (and, for the
missing-binary case, fail exactly like a real spawn failure).

Two assertions carry the weight:

* ``test_queued_run_blocks_even_when_runners_idle`` — an idle runner is
  one queued job away from being busy, and a fast-forward lands in less
  time than that.
* ``test_missing_gh_is_unknown_not_idle`` — the guard must never read
  "I could not find out" as "it is safe". That collapse is the bug that
  has bitten this codebase repeatedly.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess

from scitex_agent_container._hostsync import CiState, check_ci_idle

_IDLE_RUNNERS = (
    '{"total_count":1,"runners":[{"id":21,'
    '"name":"spartan-cpu-scitex-agent-container-01","busy":false,'
    '"labels":[{"name":"self-hosted"},{"name":"spartan-cpu"}]}]}'
)
_BUSY_RUNNERS = (
    '{"total_count":1,"runners":[{"id":21,'
    '"name":"spartan-cpu-scitex-agent-container-01","busy":true,'
    '"labels":[{"name":"self-hosted"},{"name":"spartan-cpu"}]}]}'
)
# The real fleet shape: a runner whose NAME has no host in it, and which
# is only tied to spartan by its LABEL.
_LABEL_ONLY_RUNNERS = (
    '{"total_count":1,"runners":[{"id":22,'
    '"name":"scitex-agent-container-02","busy":true,'
    '"labels":[{"name":"self-hosted"},{"name":"spartan-cpu"},'
    '{"name":"scitex-ci"}]}]}'
)
_NO_RUNS = '{"total_count":0}'
_ONE_RUN = '{"total_count":1}'


def _gh(runners: str, runs: str = _NO_RUNS):
    """A real callable answering the two gh endpoints the guard calls."""

    def run(argv, *_a, **_kw):
        url = argv[-1]
        payload = runners if "actions/runners" in url else runs
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    return run


# ---------------------------------------------------------------------------
# the safe cases
# ---------------------------------------------------------------------------


def test_idle_runners_and_no_runs_is_idle():
    # Arrange
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_IDLE_RUNNERS))
    # Assert
    assert verdict.state is CiState.IDLE


def test_idle_verdict_permits_mutation():
    # Arrange
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_IDLE_RUNNERS))
    # Assert
    assert verdict.may_mutate is True


def test_peer_with_no_runners_is_not_applicable():
    # Arrange — mba hosts no runner for this repo.
    # Act
    verdict = check_ci_idle("mba", runner=_gh(_IDLE_RUNNERS))
    # Assert
    assert verdict.state is CiState.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# busy — the checkout is the runner's audit workspace
# ---------------------------------------------------------------------------


def test_busy_runner_blocks_the_sync():
    # Arrange
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_BUSY_RUNNERS))
    # Assert
    assert verdict.state is CiState.BUSY


def test_busy_verdict_forbids_mutation():
    # Arrange
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_BUSY_RUNNERS))
    # Assert
    assert verdict.may_mutate is False


def test_busy_runner_is_named_in_the_verdict():
    # Arrange
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_BUSY_RUNNERS))
    # Assert
    assert verdict.busy_runners == ("spartan-cpu-scitex-agent-container-01",)


def test_runner_matched_by_label_not_just_name():
    # Arrange — the real fleet has runners whose name omits the host and
    # whose only tie to spartan is the `spartan-cpu` label.
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_LABEL_ONLY_RUNNERS))
    # Assert
    assert verdict.state is CiState.BUSY


def test_queued_run_blocks_even_when_runners_idle():
    # Arrange — runners idle NOW, but a job is already scheduled. It can
    # start mid-sync; "not busy" is not "will not be busy".
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_IDLE_RUNNERS, runs=_ONE_RUN))
    # Assert
    assert verdict.state is CiState.BUSY


# ---------------------------------------------------------------------------
# UNKNOWN is not IDLE
# ---------------------------------------------------------------------------


def test_missing_gh_is_unknown_not_idle():
    # Arrange — a real spawn failure, exactly as a missing binary raises.
    def no_gh(*_a, **_kw):
        raise FileNotFoundError("gh")

    # Act
    verdict = check_ci_idle("spartan", runner=no_gh)
    # Assert
    assert verdict.state is CiState.UNKNOWN


def test_unknown_verdict_forbids_mutation():
    # Arrange
    def no_gh(*_a, **_kw):
        raise FileNotFoundError("gh")

    # Act
    verdict = check_ci_idle("spartan", runner=no_gh)
    # Assert — absence of evidence is not evidence.
    assert verdict.may_mutate is False


def test_unauthenticated_gh_is_unknown():
    # Arrange — gh exits non-zero (not logged in).
    def failing_gh(argv, *_a, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: auth")

    # Act
    verdict = check_ci_idle("spartan", runner=failing_gh)
    # Assert
    assert verdict.state is CiState.UNKNOWN


def test_unparseable_gh_output_is_unknown():
    # Arrange
    def garbage_gh(argv, *_a, **_kw):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    # Act
    verdict = check_ci_idle("spartan", runner=garbage_gh)
    # Assert
    assert verdict.state is CiState.UNKNOWN


def test_unreadable_run_count_is_unknown():
    # Arrange — runners parse, but the runs endpoint answers garbage.
    # Act
    verdict = check_ci_idle("spartan", runner=_gh(_IDLE_RUNNERS, runs="{}"))
    # Assert
    assert verdict.state is CiState.UNKNOWN
