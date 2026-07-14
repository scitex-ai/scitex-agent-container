"""Tests for the single mutating operation: the fast-forward.

The invariant under test is negative and absolute — sac has NO code path
that discards a remote commit. The remote command must be
``merge --ff-only`` and nothing else; git itself then refuses a
non-fast-forward, which is the second lock on that door.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess

from scitex_agent_container._hostsync import apply_fast_forward
from scitex_agent_container._hostsync._apply import render_apply_snippet
from scitex_agent_container._state.host_config import PeerSpec

_REPO = "/data/gpfs/projects/punim0264/ywatanabe/scitex-agent-container"


def _peers(*names: str) -> dict[str, PeerSpec]:
    return {n: PeerSpec(name=n, ssh=f"user@{n}") for n in names}


def test_apply_snippet_is_fast_forward_only():
    # Arrange
    # Act
    snippet = render_apply_snippet(_REPO, "origin/develop")
    # Assert
    assert "merge --ff-only" in snippet


def test_apply_snippet_never_resets_hard():
    # Arrange — a reset would destroy remote commits, unexamined.
    # Act
    snippet = render_apply_snippet(_REPO, "origin/develop")
    # Assert
    assert "reset" not in snippet


def test_apply_snippet_never_rebases():
    # Arrange
    # Act
    snippet = render_apply_snippet(_REPO, "origin/develop")
    # Assert
    assert "rebase" not in snippet


def test_apply_snippet_targets_the_probed_checkout():
    # Arrange — the ABSOLUTE path the peer's interpreter reported, so no
    # `~` is expanded on either side.
    # Act
    snippet = render_apply_snippet(_REPO, "origin/develop")
    # Assert
    assert f"git -C {_REPO}" in snippet


def test_successful_merge_reports_ok(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="Updating aaa..bbb\nFast-forward\n")
    # Act
    result = apply_fast_forward(
        "spartan", _peers("spartan"), repo=_REPO, ref="origin/develop"
    )
    # Assert
    assert result.ok is True


def test_non_fast_forward_is_reported_as_failure(subprocess_shim):
    # Arrange — git's own refusal when HEAD is not an ancestor of the ref.
    subprocess_shim.install(
        "ssh", exit=128, stderr="fatal: Not possible to fast-forward, aborting.\n"
    )
    # Act
    result = apply_fast_forward(
        "spartan", _peers("spartan"), repo=_REPO, ref="origin/develop"
    )
    # Assert
    assert result.ok is False


def test_git_refusal_message_is_preserved_verbatim(subprocess_shim):
    # Arrange — we never paraphrase a refusal into a success.
    subprocess_shim.install(
        "ssh", exit=128, stderr="fatal: Not possible to fast-forward, aborting.\n"
    )
    # Act
    result = apply_fast_forward(
        "spartan", _peers("spartan"), repo=_REPO, ref="origin/develop"
    )
    # Assert
    assert "Not possible to fast-forward" in result.message


def test_ssh_timeout_is_a_failed_apply():
    # Arrange
    def timing_out_runner(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    # Act
    result = apply_fast_forward(
        "spartan",
        _peers("spartan"),
        repo=_REPO,
        ref="origin/develop",
        runner=timing_out_runner,
    )
    # Assert
    assert result.ok is False


def test_undefined_peer_is_a_failed_apply():
    # Arrange
    # Act
    result = apply_fast_forward(
        "ghost", _peers("spartan"), repo=_REPO, ref="origin/develop"
    )
    # Assert
    assert result.ok is False
