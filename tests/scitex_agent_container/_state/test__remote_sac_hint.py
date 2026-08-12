"""A remote-sac rc=127 must name `env_preamble` as its own remedy.

INCIDENT 2026-08-06. Cross-host lifecycle ops against a newly provisioned
compute node all failed with::

    Remote `sac agents restart compute-pilot-01` failed on 'scitex-01'
    (rc=127)
    stderr: bash: line 1: sac: command not found

sac WAS installed there; ssh runs a non-login shell, so the venv bin was not
on PATH. sac already had the fix — a peer's ``env_preamble``, which
``build_ssh_argv`` wraps into ``bash -c '<preamble> && ...'``, and which the
spartan peer has always used for exactly this. But the error never mentioned
it, so the first fix reached for was a sudo symlink into /usr/local/bin on two
hosts: a host mutation to work around a config gap.

That is what these tests pin. Not the PATH (correctly per-peer config), but
that the FAILURE NAMES ITS REMEDY, so the next person to add a host does not
repeat the worse fix.

The suppression cases matter as much as the emission case: a hint offered
when it does not apply is a confident wrong answer pointing someone at the
wrong knob, which is worse than the bare rc=127 they get today.
"""

from __future__ import annotations

from scitex_agent_container._state._remote_sac_hint import (
    remote_sac_not_found_hint,
)

_NOT_FOUND = "bash: line 1: sac: command not found\n"


class _Peer:
    def __init__(self, preamble: str = "") -> None:
        self._preamble = preamble

    def joined_preamble(self) -> str:
        return self._preamble


def _peers(preamble: str = "") -> dict:
    return {"scitex-01": _Peer(preamble)}


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def test_a_preamble_less_peer_gets_the_hint() -> None:
    """The incident case: rc=127, shell not-found, no preamble declared."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert "env_preamble" in hint


def test_the_hint_carries_a_paste_ready_config_line() -> None:
    """A direction is not enough — it must be the line that fixes it."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert "env_preamble: 'export PATH=\"$HOME/.env-sac/bin:$PATH\"'" in hint


def test_the_hint_names_the_offending_peer() -> None:
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert "scitex-01" in hint


def test_the_hint_warns_against_the_symlink_workaround() -> None:
    """The wrong fix I actually reached for, so the next reader does not."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert "/usr/local/bin" in hint


# --------------------------------------------------------------------------
# Suppression — a hint that does not apply is a wrong answer
# --------------------------------------------------------------------------


def test_a_peer_that_already_has_a_preamble_gets_no_hint() -> None:
    """Its problem is a WRONG path, not a missing one. Pointing it at
    env_preamble would send the reader to a knob already turned."""
    # Arrange
    peers = _peers('export PATH="$HOME/.env-3.11/bin:$PATH"')
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert hint == ""


def test_a_non_127_failure_gets_no_hint() -> None:
    """rc=1 from a real sac error is not a PATH problem."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 1, "boom\n", peers)
    # Assert
    assert hint == ""


def test_a_127_without_a_not_found_stderr_gets_no_hint() -> None:
    """127 alone is suggestive, not conclusive; require the shell's own words."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, "segfault\n", peers)
    # Assert
    assert hint == ""


def test_an_unknown_peer_gets_no_hint_instead_of_raising() -> None:
    """This runs on an error path already reporting a failure. A lookup
    problem here must never replace the caller's real message."""
    # Arrange
    peers = _peers()
    # Act
    hint = remote_sac_not_found_hint("no-such-peer", 127, _NOT_FOUND, peers)
    # Assert
    assert hint == ""


def test_a_peer_object_without_a_preamble_accessor_gets_no_hint() -> None:
    """Defensive: an unexpected peer shape must degrade, not crash."""
    # Arrange
    peers = {"scitex-01": object()}
    # Act
    hint = remote_sac_not_found_hint("scitex-01", 127, _NOT_FOUND, peers)
    # Assert
    assert hint == ""
