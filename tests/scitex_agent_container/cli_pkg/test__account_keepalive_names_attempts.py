"""A killed run must say WHERE it was: the peer is named before the attempt.

WHY (2026-08-15, card keepalive-intermittent-silent-failure-20260815):
one run of this command failed with ZERO journal output and then
self-recovered. StandardOutput=journal is set and successful runs do
log, so the failing run died before producing any peer-level line — and
the journal could not say which peer was being attempted. A silent
failure in the fleet's credential distribution is an outage with no
evidence of its cause.

THE PROPERTY: for every (account, peer) attempt, a line naming both
appears on stderr BEFORE the attempt runs, and the same is true of the
per-peer sweep. Outcomes (pushed / already current / FAILED / SWEEP
FAILED) follow; they never replace the pre-attempt line.
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


def _lines(result) -> list[str]:
    # click's CliRunner mixes stderr into .output by default; every
    # human-facing line of this command goes to stderr, so the mixed
    # stream carries them in order.
    return result.output.splitlines()


def test_peer_is_named_before_the_attempt(keepalive_cli, monkeypatch):
    """A run killed mid-push must leave a line naming the peer it was on."""
    from scitex_agent_container._account import token_keepalive

    # Arrange — the callback imports the seam at call time, so patching
    # the module attribute is the production path unchanged. Hand-rolled
    # fake, no mock library.
    def _dies_mid_push(account, peer, **kwargs):
        raise token_keepalive.KeepaliveError(
            f"{peer}: simulated mid-push death (nothing beyond this line)"
        )

    monkeypatch.setattr(token_keepalive, "keepalive_push", _dies_mid_push)

    # Act
    result = CliRunner().invoke(
        keepalive_cli,
        ["send-credentials", "--account", ACCOUNT, "--to", PEER],
    )

    # Assert — the attempt line exists, names peer AND account, and
    # precedes the outcome line.
    lines = _lines(result)
    pre = [i for i, ln in enumerate(lines) if "pushing access-only credential" in ln]
    assert pre, "no pre-attempt line was printed"
    assert PEER in lines[pre[0]] and ACCOUNT in lines[pre[0]]
    failed = [i for i, ln in enumerate(lines) if "FAILED" in ln]
    assert failed, "the simulated failure never rendered"
    assert pre[0] < failed[0]
    assert result.exit_code == 1


def test_peer_is_named_before_the_sweep(keepalive_cli, monkeypatch):
    """The same property holds for the post-verification sweep restart."""
    from scitex_agent_container._account import token_keepalive

    # Arrange — a clean successful push (the full record shape _render
    # consumes) so the run reaches the sweep, then a death in the sweep.
    def _pushes_cleanly(account, peer, **kwargs):
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
        raise token_keepalive.KeepaliveError(f"{peer}: simulated sweep death")

    monkeypatch.setattr(token_keepalive, "keepalive_push", _pushes_cleanly)
    monkeypatch.setattr(token_keepalive, "sweep_login_expired", _dies_in_sweep)

    # Act
    result = CliRunner().invoke(
        keepalive_cli,
        ["send-credentials", "--account", ACCOUNT, "--to", PEER, "--sweep"],
    )

    # Assert — the sweep attempt line exists, names the peer, and
    # precedes the sweep outcome line.
    lines = _lines(result)
    pre = [
        i
        for i, ln in enumerate(lines)
        if "sweeping login-expired agents" in ln
    ]
    assert pre, "no pre-sweep line was printed"
    assert PEER in lines[pre[0]]
    swept = [i for i, ln in enumerate(lines) if "SWEEP FAILED" in ln]
    assert swept, "the simulated sweep failure never rendered"
    assert pre[0] < swept[0]
    assert result.exit_code == 1
