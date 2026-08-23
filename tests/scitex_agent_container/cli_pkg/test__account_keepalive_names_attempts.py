"""A killed run must say WHERE it was: the peer is named before the attempt.

WHY (2026-08-15, card keepalive-intermittent-silent-failure-20260815):
one run of this command failed with ZERO journal output and then
self-recovered. The output goes to an append log (a drop-in redirect,
not the journal) and that log holds NO line from the failing run, so
it died before its first peer-level echo — and the record could not
say which peer was being attempted. A silent failure in the fleet's
credential distribution is an outage with no evidence of its cause.

THE PROPERTY: for every (account, peer) attempt, a line naming both
appears on stderr BEFORE the attempt runs, and the same is true of the
per-peer sweep. Outcomes (pushed / already current / FAILED / SWEEP
FAILED) follow; they never replace the pre-attempt line.

HOUSE STYLE (the repo's audit gates CI on all three):
- no pytest patch fixtures (STX-NM002): the call-time import seam is
  swapped in a yield-based fixture that restores it in ``finally`` —
  the saved_argv pattern from test__main.py — and the fakes are
  hand-rolled functions, not a mock library (STX-NM001/NM003);
- one assertion per test function (STX-TQ007): each property below is
  its own test, and the shared setup lives in the result fixtures;
- AAA marker comments on their own lines (STX-TQ002), the sibling
  file's idiom for fixture-carried results.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_keepalive import (
    register_keepalive_command,
)

PEER = "scitex-compute-04"
ACCOUNT = "alpha-example-com"


@pytest.fixture
def keepalive_cli():
    """The bare group with the keepalive command registered."""

    @click.group()
    def group():
        pass

    register_keepalive_command(group)
    return group


def _dies_mid_push(account, peer, **kwargs):
    """A hand-rolled stand-in for the push that dies before it prints."""
    from scitex_agent_container._account import token_keepalive

    raise token_keepalive.KeepaliveError(
        f"{peer}: simulated mid-push death (nothing beyond this line)"
    )


def _pushes_cleanly(account, peer, **kwargs):
    """The full record shape the command's renderer consumes."""
    return {
        "peer": peer,
        "account": account,
        "action": "pushed",
        "remote_path": "/remote/snapshot/credential",
        "mode": "600",
        "bytes": 512,
        "publish": "atomic-rename",
        "verify_status": 200,
        "previous_access_fp": None,
        "seconds_left": 3600,
        "access_fp": "f" * 64,
    }


def _dies_in_sweep(peer):
    """A hand-rolled stand-in for the sweep restart that dies."""
    from scitex_agent_container._account import token_keepalive

    raise token_keepalive.KeepaliveError(f"{peer}: simulated sweep death")


@pytest.fixture
def mid_push_death_result(keepalive_cli):
    """One run whose push seam dies; the seam is restored on teardown."""
    from scitex_agent_container._account import token_keepalive

    original = token_keepalive.keepalive_push
    token_keepalive.keepalive_push = _dies_mid_push
    try:
        yield CliRunner().invoke(
            keepalive_cli,
            ["send-credentials", "--account", ACCOUNT, "--to", PEER],
        )
    finally:
        token_keepalive.keepalive_push = original


@pytest.fixture
def sweep_death_result(keepalive_cli):
    """One run that pushes cleanly, then dies in the sweep restart.

    The seam is restored on teardown (both of them).
    """
    from scitex_agent_container._account import token_keepalive

    original_push = token_keepalive.keepalive_push
    original_sweep = token_keepalive.sweep_login_expired
    token_keepalive.keepalive_push = _pushes_cleanly
    token_keepalive.sweep_login_expired = _dies_in_sweep
    try:
        yield CliRunner().invoke(
            keepalive_cli,
            ["send-credentials", "--account", ACCOUNT, "--to", PEER, "--sweep"],
        )
    finally:
        token_keepalive.keepalive_push = original_push
        token_keepalive.sweep_login_expired = original_sweep


def _lines(result) -> list[str]:
    # click's CliRunner mixes stderr into .output by default; every
    # human-facing line of this command goes to stderr, so the mixed
    # stream carries them in order.
    return result.output.splitlines()


def _index_of(lines: list[str], marker: str) -> int:
    return next(i for i, ln in enumerate(lines) if marker in ln)


def test_killed_push_prints_a_pre_attempt_line(mid_push_death_result):
    """A run killed mid-push still leaves the pre-attempt line."""
    # Arrange: see fixture
    result = mid_push_death_result

    # Act
    output = result.output

    # Assert
    assert "pushing access-only credential" in output


def test_killed_push_pre_attempt_line_names_peer_and_account(mid_push_death_result):
    """The pre-attempt line names BOTH the peer and the account."""
    # Arrange: see fixture
    result = mid_push_death_result

    # Act
    pre = [ln for ln in _lines(result) if "pushing access-only credential" in ln]

    # Assert
    assert pre and PEER in pre[0] and ACCOUNT in pre[0]


def test_killed_push_pre_attempt_line_precedes_failure_line(mid_push_death_result):
    """The pre-attempt line comes BEFORE the outcome, not instead of it."""
    # Arrange: see fixture
    result = mid_push_death_result

    # Act
    lines = _lines(result)
    pre = _index_of(lines, "pushing access-only credential")
    failed = _index_of(lines, "FAILED")

    # Assert
    assert pre < failed


def test_killed_push_exits_non_zero(mid_push_death_result):
    """The run still fails loudly — the pre-attempt line is not a softener."""
    # Arrange: see fixture
    result = mid_push_death_result

    # Act
    exit_code = result.exit_code

    # Assert
    assert exit_code == 1


def test_killed_sweep_prints_a_pre_sweep_line(sweep_death_result):
    """The same property holds for the post-verification sweep restart."""
    # Arrange: see fixture
    result = sweep_death_result

    # Act
    output = result.output

    # Assert
    assert "sweeping login-expired agents" in output


def test_killed_sweep_pre_sweep_line_names_peer(sweep_death_result):
    """The pre-sweep line names the peer being swept."""
    # Arrange: see fixture
    result = sweep_death_result

    # Act
    pre = [ln for ln in _lines(result) if "sweeping login-expired agents" in ln]

    # Assert
    assert pre and PEER in pre[0]


def test_killed_sweep_pre_sweep_line_precedes_failure_line(sweep_death_result):
    """The pre-sweep line comes BEFORE the sweep outcome line."""
    # Arrange: see fixture
    result = sweep_death_result

    # Act
    lines = _lines(result)
    pre = _index_of(lines, "sweeping login-expired agents")
    swept = _index_of(lines, "SWEEP FAILED")

    # Assert
    assert pre < swept


def test_killed_sweep_exits_non_zero(sweep_death_result):
    """The sweep failure still fails the run."""
    # Arrange: see fixture
    result = sweep_death_result

    # Act
    exit_code = result.exit_code

    # Assert
    assert exit_code == 1
