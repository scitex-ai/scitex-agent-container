"""A peer's ``env_preamble`` must not change what ``command`` MEANS.

MEASURED 2026-08-17, reported by scitex-cards with a control. Every peer
carrying an ``env_preamble`` returned rc=127 for any dispatched command::

    scitex-compute-03   preamble=yes   rc=127
    scitex-compute-02   preamble=yes   rc=127
    scitex-nas-03       preamble=no    rc=0    'REACHED'

    bash: line 1: sh -c 'echo REACHED': command not found

That took scitex-hub down and unstartable by ANY path — it is pinned to
compute-03, and both ``agent_start`` from a container and ``agent_spawn``
through the host broker funnel here. It also blocked a scitex-cards client
rollout, because the two affected hosts could not be reached to do it.

THE DEFECT WAS THAT THE TWO BRANCHES DISAGREED ABOUT WHAT ``command`` IS:

    preamble branch   shlex.join  -> a REAL ARGV LIST
    bare branch       raw append  -> ssh space-joins, so it had to arrive
                                     ALREADY SHELL-QUOTED

No caller could satisfy both. ``_spec_handoff`` pre-quoted its script into one
element to make the bare branch work; the preamble branch then quoted that
already-quoted element a second time, and the remote bash looked for a FILE by
that name. Each layer's docstring correctly explained why IT quoted; neither
knew the other did too.

THE FIX IS THAT THE BUILDER OWNS QUOTING IN BOTH BRANCHES, so ``command`` is a
real argv list everywhere — which is what every other caller in the repo
already passed (``["sac", "agents", "start", name, "--json"]`` and friends).

WHY THESE TESTS ARE A 2x2 rather than the one broken case: the bug was an
ASYMMETRY, so a fix verified only on the preamble side could silently re-break
the bare side. Both peer kinds are checked against both a plain command and one
whose argument CONTAINS WHITESPACE — the case that proves quoting survives, and
the case a space-joining "fix" gets wrong (it reflows into two arguments).

PA-306: no mocks. A real config file through the production
``$SCITEX_AGENT_CONTAINER_CONFIG`` seam and the real argv builder. Nothing is
executed — argv rendering is the whole subject.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from scitex_agent_container._state._host_ssh import build_ssh_argv
from scitex_agent_container._state.host_config import load as load_host_config

_PREAMBLE_PEER = "withpreamble"
_BARE_PEER = "nopreamble"
_PREAMBLE = 'export PATH="$HOME/.env-sac/bin:$PATH"'

#: The `_spec_handoff` shape, now a real argv list rather than a pre-quoted blob.
_SH_C = ["sh", "-c", "echo REACHED"]

#: An argument containing whitespace — the case that pins quoting.
_SPACED = ["echo", "hello world"]


@pytest.fixture
def peers(tmp_path: Path, env_save_restore):
    """A real config with one preamble peer and one bare peer."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
peers:
  {_PREAMBLE_PEER}:
    ssh: host-with-preamble
    env_preamble: ['{_PREAMBLE}']
  {_BARE_PEER}: {{ ssh: host-bare }}
"""
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return load_host_config().peers


def _remote_command(argv: list[str]) -> str:
    """The command line the REMOTE shell ends up parsing, for either branch.

    Both branches now collapse into a single final element — ``bash -c
    <quoted>`` when a preamble applies, the shlex-joined command otherwise.
    Recovering the remote command line for both is what lets one assertion span
    the asymmetry that caused the bug.
    """
    if argv[-1].startswith("bash -c "):
        return shlex.split(argv[-1])[2]
    return argv[-1]


# ---------------------------------------------------------------------------
# The regression: `sh -c <script>` reaches the remote intact on BOTH peers.
# ---------------------------------------------------------------------------


def test_a_shell_script_survives_a_preamble_peer(peers):
    """The outage. Double-quoting made the remote bash look for a file."""
    # Arrange
    command = list(_SH_C)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.endswith("sh -c 'echo REACHED'")


def test_a_shell_script_survives_a_bare_peer(peers):
    """The side that worked, pinned so a one-sided fix cannot break it."""
    # Arrange
    command = list(_SH_C)
    # Act
    remote = _remote_command(build_ssh_argv(_BARE_PEER, command, peers))
    # Assert
    assert remote.endswith("sh -c 'echo REACHED'")


# ---------------------------------------------------------------------------
# Whitespace inside an argument — what a space-joining "fix" gets wrong.
# ---------------------------------------------------------------------------


def test_a_spaced_argument_stays_one_token_for_a_preamble_peer(peers):
    """`hello world` is ONE argument and must not reflow into two."""
    # Arrange
    command = list(_SPACED)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.endswith("echo 'hello world'")


def test_a_spaced_argument_stays_one_token_for_a_bare_peer(peers):
    """NEW protection. This branch used to append raw tokens and lose the quoting.

    Completes the 2x2. Before the fix a spaced argument was preserved on the
    preamble side and silently reflowed on the bare side — the same asymmetry
    as the outage, pointing the other way, and untested.
    """
    # Arrange
    command = list(_SPACED)
    # Act
    remote = _remote_command(build_ssh_argv(_BARE_PEER, command, peers))
    # Assert
    assert remote.endswith("echo 'hello world'")


# ---------------------------------------------------------------------------
# The preamble must still actually run, and run FIRST.
# ---------------------------------------------------------------------------


def test_the_preamble_still_runs_before_the_command(peers):
    """Quoting correctly is worthless if the preamble stopped applying.

    Asserted as ordering, not mere presence: the preamble exists to put
    `apptainer`/`sac` on PATH, so running it AFTER the command would satisfy a
    contains-check and still fail every dispatch.
    """
    # Arrange
    command = list(_SH_C)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.index(_PREAMBLE) < remote.index("sh -c")


def test_a_bare_peer_gets_no_preamble_wrapper(peers):
    """A peer without env_preamble must not gain a bash -c wrapper."""
    # Arrange
    command = list(_SH_C)
    # Act
    argv = build_ssh_argv(_BARE_PEER, command, peers)
    # Assert
    assert not argv[-1].startswith("bash -c ")
