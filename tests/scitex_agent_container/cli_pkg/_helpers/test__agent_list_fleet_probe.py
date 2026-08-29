"""Tests for the ssh leg of the fleet listing, and for target enumeration.

No mocks and no ``monkeypatch``: ``ssh_peer_probe``'s ``runner`` seam is a real
callable with ``subprocess.run``'s signature, so every rc / timeout / legacy-peer
branch is driven through the production mapping rather than around it.
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import (
    INSTRUMENT_SSH,
    MALFORMED,
    RESPONDED,
    SAC_MISSING,
    TIMED_OUT,
    UNREACHABLE,
    HostTarget,
    resolve_targets,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_probe import (
    ssh_peer_probe,
)

LOCAL = "test-local-host"
_PEER_NAME = "mba"
_TARGET = HostTarget(name=_PEER_NAME, ssh=_PEER_NAME)


class _Peer:
    """The one attribute ``resolve_targets`` / ``build_ssh_argv`` read."""

    def __init__(self, ssh: str) -> None:
        self.name = ssh or "unnamed"
        self.ssh = ssh
        self.via: tuple[str, ...] = ()

    def jump_chain(self, peers):
        return []

    def joined_preamble(self) -> str:
        return ""


_PEERS = {_PEER_NAME: _Peer(_PEER_NAME)}


class _Proc:
    """The three attributes the probe reads off a CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(*results, record: list | None = None):
    """A real callable with ``subprocess.run``'s signature, answering in turn."""
    queue = list(results)

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run


def _probe(*results, record: list | None = None, **kwargs):
    return ssh_peer_probe(
        _TARGET,
        8.0,
        peers=_PEERS,
        runner=_runner(*results, record=record),
        **kwargs,
    )


# ===========================================================================
# The happy path
# ===========================================================================


def test_ssh_probe_reports_the_host_as_responded():
    # Arrange
    proc = _Proc(0, '{"agents": [{"name": "remote-agent"}]}')
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.status == RESPONDED


def test_ssh_probe_names_ssh_as_the_instrument():
    # Arrange
    proc = _Proc(0, '{"agents": []}')
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.instrument == INSTRUMENT_SSH


def test_ssh_probe_returns_the_peers_rows():
    # Arrange
    proc = _Proc(0, '{"agents": [{"name": "remote-agent"}]}')
    # Act
    _, rows = _probe(proc)
    # Assert
    assert [r["name"] for r in rows] == ["remote-agent"]


def test_ssh_probe_accepts_the_bare_list_shape_too():
    # Arrange -- print_agent_list_json emits a bare list, not the envelope.
    proc = _Proc(0, '[{"name": "remote-agent"}]')
    # Act
    _, rows = _probe(proc)
    # Assert
    assert [r["name"] for r in rows] == ["remote-agent"]


def test_ssh_probe_counts_the_agents_it_actually_received():
    # Arrange
    proc = _Proc(0, '{"agents": [{"name": "a"}, {"name": "b"}]}')
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.agents == 2


# ===========================================================================
# A remote row must name its MACHINE, not the peer's own point of view
# ===========================================================================


def test_a_remote_row_is_stamped_with_the_machine_not_the_peers_own_local():
    # Arrange -- the peer describes its agents as host="local": true THERE,
    # a lie HERE, and a whole column of "local" is the original problem.
    proc = _Proc(0, '{"agents": [{"name": "a", "host": "local"}]}')
    # Act
    _, rows = _probe(proc)
    # Assert
    assert rows[0]["host"] == _PEER_NAME


def test_a_remote_rows_display_host_is_stamped_too():
    # Arrange
    proc = _Proc(0, '{"agents": [{"name": "a", "host": "local"}]}')
    # Act
    _, rows = _probe(proc)
    # Assert
    assert rows[0]["host_display"] == _PEER_NAME


def test_a_remote_row_keeps_the_peers_own_resolved_hostname_when_it_has_one():
    # Arrange
    proc = _Proc(
        0,
        '{"agents": [{"name": "a", "host": "local", '
        '"host_display": "ywata-note-win"}]}',
    )
    # Act
    _, rows = _probe(proc)
    # Assert
    assert rows[0]["host"] == "ywata-note-win"


# ===========================================================================
# Failure modes stay DISTINCT -- each one has a different remedy
# ===========================================================================


def test_ssh_timeout_is_reported_as_timed_out_not_unreachable():
    # Arrange -- a slow host has not refused us.
    expired = subprocess.TimeoutExpired(cmd="ssh", timeout=8.0)
    # Act
    report, _ = _probe(expired)
    # Assert
    assert report.status == TIMED_OUT


def test_a_timed_out_probe_says_how_long_it_waited():
    # Arrange
    expired = subprocess.TimeoutExpired(cmd="ssh", timeout=8.0)
    # Act
    report, _ = _probe(expired)
    # Assert
    assert "timed out after 8s" in report.detail


def test_a_timed_out_probe_returns_no_rows():
    # Arrange
    expired = subprocess.TimeoutExpired(cmd="ssh", timeout=8.0)
    # Act
    _, rows = _probe(expired)
    # Assert
    assert rows == []


def test_a_nonzero_ssh_exit_is_reported_as_unreachable():
    # Arrange
    proc = _Proc(255, "", "ssh: connect to host x port 22: No route to host")
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.status == UNREACHABLE


def test_an_unreachable_host_carries_the_ssh_error_as_its_reason():
    # Arrange
    proc = _Proc(255, "", "ssh: connect to host x port 22: No route to host")
    # Act
    report, _ = _probe(proc)
    # Assert
    assert "No route to host" in report.detail


def test_a_host_we_reached_without_sac_is_not_called_unreachable():
    # Arrange -- measured live on two NAS boxes: we arrived, sac was missing,
    # and the remedy is "install sac there", not "fix the network".
    proc = _Proc(127, "", "sh: sac: command not found")
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.status == SAC_MISSING


def test_an_unparseable_answer_is_malformed_not_unreachable():
    # Arrange -- the transport demonstrably worked.
    proc = _Proc(0, "Welcome to Ubuntu!\n")
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.status == MALFORMED


def test_an_ssh_binary_that_cannot_run_is_reported_not_raised():
    # Arrange
    missing = FileNotFoundError("no ssh here")
    # Act
    report, _ = _probe(missing)
    # Assert
    assert report.status == UNREACHABLE


# ===========================================================================
# The recursion guard, and the stale-peer retry
# ===========================================================================


def test_the_peer_leg_passes_the_recursion_guard():
    # Arrange -- without it the peer's own sac would fan out again.
    seen: list = []
    # Act
    _probe(_Proc(0, '{"agents": []}'), record=seen)
    # Assert
    assert "--no-fanout" in seen[0]


def test_a_peer_on_an_older_sac_is_retried_without_the_guard():
    # Arrange -- an old sac rejects the flag, which PROVES it cannot recurse.
    seen: list = []
    # Act
    _probe(
        _Proc(2, "", "Error: No such option: --no-fanout"),
        _Proc(0, '{"agents": [{"name": "old-peer-agent"}]}'),
        record=seen,
    )
    # Assert
    assert "--no-fanout" not in seen[1]


def test_a_stale_peer_still_reads_as_responded_after_the_retry():
    # Arrange -- without the retry every not-yet-upgraded peer in the fleet
    # would read UNREACHABLE, which is the false negative this feature kills.
    # Act
    report, _ = _probe(
        _Proc(2, "", "Error: No such option: --no-fanout"),
        _Proc(0, '{"agents": [{"name": "old-peer-agent"}]}'),
    )
    # Assert
    assert report.status == RESPONDED


def test_a_stale_peers_rows_are_still_returned():
    # Arrange
    # Act
    _, rows = _probe(
        _Proc(2, "", "Error: No such option: --no-fanout"),
        _Proc(0, '{"agents": [{"name": "old-peer-agent"}]}'),
    )
    # Assert
    assert [r["name"] for r in rows] == ["old-peer-agent"]


def test_the_capability_filter_travels_to_the_peer():
    # Arrange -- filtered at the source, not shipped back and discarded.
    seen: list = []
    # Act
    _probe(_Proc(0, '{"agents": []}'), record=seen, capability="HPC")
    # Assert
    assert seen[0][-2:] == ["--capability", "HPC"]


def test_the_group_filter_travels_to_the_peer():
    # Arrange
    seen: list = []
    # Act
    _probe(_Proc(0, '{"agents": []}'), record=seen, group="active")
    # Assert
    assert seen[0][-2:] == ["--group", "active"]


def test_the_verbose_view_choice_does_not_travel_to_the_peer():
    # Arrange -- what the READER sees is decided locally, where every host's
    # rows are held at once.
    seen: list = []
    # Act
    _probe(_Proc(0, '{"agents": []}'), record=seen)
    # Assert
    assert "-v" not in seen[0]


# ===========================================================================
# Target enumeration
# ===========================================================================


def test_the_local_host_is_the_first_target():
    # Arrange
    peers = {"mba": _Peer("mba")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert targets[0].local is True


def test_glob_peer_keys_are_not_queried():
    # Arrange -- `spartan-*` is a PATTERN; there is no one machine to ask.
    peers = {"spartan": _Peer("spartan"), "spartan-*": _Peer("")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert [t.name for t in targets] == [LOCAL, "spartan"]


def test_the_local_host_is_never_also_queried_as_a_peer():
    # Arrange -- this fleet's config.yaml lists the local box among its peers.
    peers = {LOCAL: _Peer(LOCAL), "mba": _Peer("mba")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert [t.name for t in targets] == [LOCAL, "mba"]


def test_two_peer_keys_sharing_one_ssh_route_collapse_to_one_target():
    # Arrange -- `nas-03` (config.yaml) and `scitex-nas-03` (registry) render
    # the identical ssh argv; querying both would print every agent twice.
    peers = {"nas-03": _Peer("scitex-nas-03"), "scitex-nas-03": _Peer("scitex-nas-03")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert [t.name for t in targets] == [LOCAL, "nas-03"]


def test_the_collapsed_peer_key_survives_as_an_alias():
    # Arrange
    peers = {"nas-03": _Peer("scitex-nas-03"), "scitex-nas-03": _Peer("scitex-nas-03")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert targets[1].aliases == ("scitex-nas-03",)


def test_a_peer_with_no_ssh_route_is_not_offered_as_a_target():
    # Arrange -- the registry records ssh_alias null for ywata-note-win.
    peers = {"unroutable": _Peer("")}
    # Act
    targets = resolve_targets(peers, local_host=LOCAL)
    # Assert
    assert [t.name for t in targets] == [LOCAL]


def test_an_unreadable_peer_topology_still_yields_the_local_host():
    # Arrange -- a listing must never crash because config.yaml is broken.
    broken: dict = {}
    # Act
    targets = resolve_targets(broken, local_host=LOCAL)
    # Assert
    assert [t.name for t in targets] == [LOCAL]


@pytest.mark.parametrize("rc", [126, 127])
def test_a_shell_that_could_not_launch_sac_is_distinguished_by_its_message(rc):
    # Arrange
    proc = _Proc(rc, "", "sh: sac: command not found")
    # Act
    report, _ = _probe(proc)
    # Assert
    assert report.status == SAC_MISSING
