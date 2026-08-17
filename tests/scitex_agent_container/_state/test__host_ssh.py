"""``command`` means a real argv list, quoted exactly ONCE by the builder.

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

THE MECHANISM — the two branches DISAGREED about what ``command`` means. ssh
word-joins everything after the host and hands the result to the remote shell.
The preamble branch treated ``command`` as a real argv list (``shlex.join``);
the bare branch treated it as tokens the caller had already quoted. No caller
could satisfy both. ``_spec_handoff`` pre-quoted for the bare branch, passing
one element ``sh -c '...'``, and the preamble branch then quoted it a SECOND
time — so the remote bash saw a single word and looked for a FILE by that name.
Each layer's docstring explained why IT quoted. Neither knew the other did.

THE FIRST FIX made the preamble branch space-join to match the bare branch.
Consistent, and consistently WRONG: it exported the bare branch's long-standing
inability to carry a whitespace-bearing argument onto the preamble peers,
breaking a live caller that passes ``["python3", "-c", "<one-liner>"]`` (A/B on
real hosts: compute-03 parent rc=0, that fix rc=2 syntax error). THE DECIDING
MEASUREMENT was the BARE peer in that same A/B — nas-03 failed on BOTH
renderings. That branch had never carried a quoted argument at all, so the
defect sat there silently while looking like a preamble-only problem.

THE FIX IS QUOTE-ONCE-IN-THE-BUILDER. ``command`` now means ONE thing on both
sides: a real argv list whose quoting ``build_ssh_argv`` owns, emitted as a
single ``shlex``-joined element so ssh's word-join puts exactly the intended
command line on the wire. Callers pass real argv lists and stop pre-quoting.

WHAT THESE TESTS PIN, and why it is a 2x2 rather than the one broken case: the
bug was an ASYMMETRY between the two branches, so a fix verified only on the
preamble side can silently re-break the bare side — and the first fix proved
the reverse direction is just as easy. Both command shapes in real use are
covered against both peer kinds:

    argv with a whitespace-bearing argument   the ``_spec_handoff`` and
                                              creds-probe shape
    argv of plain words                       the ``priority_cmds`` shape

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

#: The `_spec_handoff` shape: a real argv list whose LAST element is a whole
#: script — spaces and all. This is the element the builder must quote, and
#: must quote exactly once.
_SCRIPT_ARGV = ["sh", "-c", "echo REACHED"]

#: The `priority_cmds` shape: a real argv list of plain words.
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

    Both branches collapse into a single trailing element — the preamble
    branch a ``bash -c <quoted>`` wrapper, the bare branch the shlex-joined
    command itself — precisely so ssh's post-host word-join cannot disturb
    the quoting. Recovering the remote command line for both is what lets one
    assertion span the asymmetry that caused the bug.
    """
    if argv[-1].startswith("bash -c "):
        return shlex.split(argv[-1])[2]
    return argv[-1]


# ---------------------------------------------------------------------------
# The regression: the caller's argv round-trips through BOTH peer kinds.
# ---------------------------------------------------------------------------


def test_a_script_argument_is_quoted_exactly_once_for_a_preamble_peer(peers):
    """The outage. Double-quoting made the remote bash look for a file.

    Asserted as a ROUND-TRIP — re-parse the remote command line and get the
    caller's argv back — because that is the property the two failed attempts
    each broke in a different direction: quoting twice fuses the argv into one
    word, quoting zero times splits the script argument into several. Only
    quoting exactly once reproduces `["sh", "-c", "echo REACHED"]` remotely.
    """
    # Arrange
    command = list(_SCRIPT_ARGV)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert shlex.split(remote)[-len(_SCRIPT_ARGV) :] == _SCRIPT_ARGV


def test_a_script_argument_is_quoted_exactly_once_for_a_bare_peer(peers):
    """The branch the deciding measurement condemned, now pinned.

    This side LOOKED healthy through the outage because nothing ever handed it
    a whitespace-bearing argument; nas-03 then failed the live A/B on both
    candidate renderings. Same round-trip property as the preamble case — that
    the two branches now agree on it is the whole fix.
    """
    # Arrange
    command = list(_SCRIPT_ARGV)
    # Act
    remote = _remote_command(build_ssh_argv(_BARE_PEER, command, peers))
    # Assert
    assert shlex.split(remote)[-len(_SCRIPT_ARGV) :] == _SCRIPT_ARGV


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
    command = list(_SCRIPT_ARGV)
    # Act
    remote = _remote_command(build_ssh_argv(_PREAMBLE_PEER, command, peers))
    # Assert
    assert remote.index(_PREAMBLE) < remote.index("echo REACHED")


def test_a_bare_peer_gets_no_preamble_wrapper(peers):
    """A peer without env_preamble must not acquire a `bash -c` wrapper."""
    # Arrange
    command = list(_SCRIPT_ARGV)
    # Act
    argv = build_ssh_argv(_BARE_PEER, command, peers)
    # Assert
    assert not argv[-1].startswith("bash -c ")
