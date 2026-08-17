"""A peer's ``env_preamble`` must not change how its command is quoted.

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

THE MECHANISM — two individually-correct quotings that compose wrongly. ssh
word-joins everything after the host and hands the result to the remote shell,
so a caller wanting ONE remote token must pre-quote it; ``_spec_handoff``
correctly does, passing a single element ``sh -c '...'``. The preamble branch
then ran ``shlex.join`` over that already-quoted element, quoting it a second
time, so the remote bash saw one word and looked for a FILE by that name.
Each layer's docstring explained why IT quoted. Neither knew the other did.

WHAT THESE TESTS PIN, and why it is a 2x2 rather than the one broken case:
the bug was an ASYMMETRY between the two branches, so a fix verified only on
the preamble side could silently re-break the bare side that works today.
Both command shapes in real use are covered against both peer kinds:

    pre-quoted single element   the ``_spec_handoff`` shape
    real argv list              the ``priority_cmds`` shape

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

#: The `_spec_handoff` shape: ONE element the caller already quoted.
_PREQUOTED = "sh -c 'echo REACHED'"

#: The `priority_cmds` shape: a real argv list.
_ARGV_LIST = ["echo", "REACHED"]


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
    """What the REMOTE shell ends up parsing, for either branch.

    The preamble branch collapses into a single ``bash -c <quoted>`` element;
    the bare branch leaves tokens that ssh will space-join. Recovering the
    remote command line for both is what lets one assertion span the asymmetry
    that caused the bug.
    """
    if argv[-1].startswith("bash -c "):
        return shlex.split(argv[-1])[2]
    # Bare branch: ssh joins every token after `--` with spaces.
    return " ".join(argv[argv.index("--") + 1 :])


# ---------------------------------------------------------------------------
# The regression: a pre-quoted element survives BOTH peer kinds intact.
# ---------------------------------------------------------------------------


def test_a_prequoted_command_is_not_requoted_for_a_preamble_peer(peers):
    """The bug. Double-quoting made the remote bash look for a file."""
    # Arrange
    command = [_PREQUOTED]
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.endswith(_PREQUOTED)


def test_a_prequoted_command_is_not_requoted_for_a_bare_peer(peers):
    """The side that worked, pinned so a one-sided fix cannot break it."""
    # Arrange
    command = [_PREQUOTED]
    # Act
    remote = _remote_command(build_ssh_argv(_BARE_PEER, command, peers))
    # Assert
    assert remote.endswith(_PREQUOTED)


def test_a_real_argv_list_survives_a_preamble_peer(peers):
    """The other command shape in production use — priority_cmds passes lists."""
    # Arrange
    command = list(_ARGV_LIST)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.endswith("echo REACHED")


def test_a_real_argv_list_survives_a_bare_peer(peers):
    """Completes the 2x2. The asymmetry between branches was the whole bug."""
    # Arrange
    command = list(_ARGV_LIST)
    # Act
    remote = _remote_command(build_ssh_argv(_BARE_PEER, command, peers))
    # Assert
    assert remote.endswith("echo REACHED")


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
    command = [_PREQUOTED]
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.index(_PREAMBLE) < remote.index("echo REACHED")


def test_a_bare_peer_gets_no_preamble_wrapper(peers):
    """A peer without env_preamble must keep its byte-identical argv shape."""
    # Arrange
    command = [_PREQUOTED]
    # Act
    argv = build_ssh_argv(_BARE_PEER, command, peers)
    # Assert
    assert not argv[-1].startswith("bash -c ")
