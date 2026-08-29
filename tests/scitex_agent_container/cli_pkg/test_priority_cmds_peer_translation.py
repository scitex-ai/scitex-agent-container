"""`priority_cmds` must translate a peer NAME into its ssh ALIAS.

MEASURED 2026-08-17. `spec.host` names a PEER; sac's peer table maps that name
to an ssh ALIAS, and for the fleet's laptop the two resolve to different
machines:

    peer name        ywata-note-win
    ssh alias        ywata-note-win-net  -> bastion-win.scitex.ai   (reaches)
    the BARE name                        -> 192.168.11.101          (dead)

This module handed the raw `spec.host` straight to ssh. So for every agent
pinned to that peer — 54 of them — it reported "preferred host unreachable"
while sac's OWN dispatch reached the same machine fine, because dispatch
translates and this did not. Two consumers, one string, opposite outcomes.

Not merely a bad report: `_ssh_start_agent` fed the same untranslated name to a
REMOTE START, so a yield decision taken on a false "unreachable" would have
tried to start an agent at an address that goes nowhere.

WHAT THESE TESTS ASSERT, and why it is the report rather than the mechanism:
the ssh TARGET must be the alias and must NOT be the bare peer name. That is a
property of the rendered argv, so it holds however the translation is
implemented — a test keyed to "does it call build_ssh_argv" would pass a
reimplementation that called it wrongly.

PA-306: no mocks. A real config file through the production
`$SCITEX_AGENT_CONTAINER_CONFIG` seam, and the real argv builder. Nothing is
executed — argv rendering is the whole subject.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._priority_ssh import (
    _PROBE_WALL_TIMEOUT,
    _SSH_PROBE_OPTS,
)
from scitex_agent_container.cli_pkg.priority_cmds import _peer_ssh_argv

# The incident's own pair: a peer whose name is NOT its ssh alias.
_PEER = "ywata-note-win"
_ALIAS = "ywata-note-win-net"

_OPTS = ["-o", "BatchMode=yes"]


@pytest.fixture
def peer_table(tmp_path: Path, env_save_restore) -> None:
    """A real config file with one aliased peer and one jump-chained peer."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
peers:
  {_PEER}: {{ ssh: {_ALIAS} }}
  mba: {{ ssh: ywatanabe@mba.local }}
  behind-jump:
    ssh: deep-host
    via: [mba]
"""
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))


# ---------------------------------------------------------------------------
# The regression.
# ---------------------------------------------------------------------------


def test_a_registered_peer_is_reached_by_its_ssh_alias(peer_table):
    """The fix: the ssh target is the ALIAS the peer table declares."""
    # Arrange
    command = ["hostname"]
    # Act
    argv = _peer_ssh_argv(_PEER, command, _OPTS)
    # Assert
    assert _ALIAS in argv


def test_the_bare_peer_name_is_never_the_ssh_target(peer_table):
    """The bug, asserted from the losing side.

    `ywata-note-win` as an ssh target resolves to a dead LAN address. Its
    presence anywhere in the argv as a standalone token would mean the
    translation did not happen.
    """
    # Arrange
    command = ["hostname"]
    # Act
    argv = _peer_ssh_argv(_PEER, command, _OPTS)
    # Assert
    assert _PEER not in argv


def test_the_command_survives_translation(peer_table):
    """Translating the host must not drop what we came to run."""
    # Arrange
    command = ["sac", "agent", "start", "figrecipe"]
    # Act
    argv = _peer_ssh_argv(_PEER, command, _OPTS)
    # Assert
    assert "figrecipe" in argv


def test_the_caller_supplied_ssh_options_survive_translation(peer_table):
    """BatchMode/ConnectTimeout are why the probe cannot hang. Keep them."""
    # Arrange
    command = ["hostname"]
    # Act
    argv = _peer_ssh_argv(_PEER, command, _OPTS)
    # Assert
    assert "BatchMode=yes" in argv


def test_a_jump_chained_peer_renders_a_proxy_jump(peer_table):
    """Proves we went through the real builder, not a lookalike.

    A hand-rolled `["ssh", alias, cmd]` would pass every assertion above and
    still strand any peer that needs a bastion. `-J` only appears if the
    production chain logic ran.
    """
    # Arrange
    command = ["hostname"]
    # Act
    argv = _peer_ssh_argv("behind-jump", command, _OPTS)
    # Assert
    assert "-J" in argv


# ---------------------------------------------------------------------------
# The timeout the fix silently changed. Pinned so it cannot change again
# without a test saying so.
# ---------------------------------------------------------------------------


def _first_connect_timeout(argv: list[str]) -> int:
    """The ConnectTimeout ssh will actually honour — the FIRST one present.

    Rendered argv can legitimately carry the option twice (the builder emits
    its own defaults, then appends the caller's). ssh takes the first, so
    reading "is ConnectTimeout=3 in argv" would answer the wrong question:
    it is present AND ignored.
    """
    for token in argv:
        if token.startswith("ConnectTimeout="):
            return int(token.split("=", 1)[1])
    raise AssertionError(f"no ConnectTimeout in argv: {argv}")


def test_a_registered_peer_uses_the_fleet_standard_connect_timeout(peer_table):
    """Routing through the builder means the builder's timeout wins.

    This module's private ConnectTimeout=3 is still APPENDED, and is still
    inert, because build_ssh_argv emits its defaults first. Asserting the
    effective value rather than mere presence is the whole point.
    """
    # Arrange
    command = ["hostname"]
    # Act
    argv = _peer_ssh_argv(_PEER, command, _SSH_PROBE_OPTS)
    # Assert
    assert _first_connect_timeout(argv) == 10


def test_an_unregistered_host_keeps_the_short_probe_timeout(peer_table):
    """The pass-through path is untouched — there our 3s is the only copy."""
    # Arrange
    stranger = "some-host-not-in-the-table"
    # Act
    argv = _peer_ssh_argv(stranger, ["hostname"], _SSH_PROBE_OPTS)
    # Assert
    assert _first_connect_timeout(argv) == 3


def test_the_wall_clock_cap_fires_before_the_ssh_timeout(peer_table):
    """Why the slower ssh timeout does not change the VERDICT, only latency.

    `probe_ssh` bounds the call with subprocess `timeout=_PROBE_WALL_TIMEOUT`
    and treats expiry as unreachable. As long as that cap is shorter than the
    ssh ConnectTimeout, an unreachable registered peer still answers False —
    it just costs the cap instead of 3s. If someone later raises the cap above
    the ssh timeout, this inverts and the argument in the docstring stops
    holding, so the relationship is asserted rather than described.
    """
    # Arrange
    argv = _peer_ssh_argv(_PEER, ["hostname"], _SSH_PROBE_OPTS)
    # Act
    ssh_timeout = _first_connect_timeout(argv)
    # Assert
    assert _PROBE_WALL_TIMEOUT < ssh_timeout


# ---------------------------------------------------------------------------
# The non-regression: hosts the table does not know must keep working.
# ---------------------------------------------------------------------------


def test_an_unregistered_host_is_passed_through_unchanged(peer_table):
    """It may still resolve via ~/.ssh/config, and that path works today.

    Translating only what the table knows fixes the defect without breaking
    the case it never covered.
    """
    # Arrange
    stranger = "some-host-not-in-the-table"
    # Act
    argv = _peer_ssh_argv(stranger, ["hostname"], _OPTS)
    # Assert
    assert stranger in argv


def test_an_unregistered_host_still_gets_its_command(peer_table):
    # Arrange
    stranger = "some-host-not-in-the-table"
    # Act
    argv = _peer_ssh_argv(stranger, ["hostname"], _OPTS)
    # Assert
    assert "hostname" in argv


def test_an_absent_peer_table_degrades_to_pass_through(env_save_restore, tmp_path):
    """An unreadable config must not break a probe that works without one."""
    # Arrange
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "does-not-exist.yaml")
    )
    # Act
    argv = _peer_ssh_argv("anything", ["hostname"], _OPTS)
    # Assert
    assert "anything" in argv
