"""Tests for the fleet-wide spec-source drift check.

PA-306: no mocks. The ssh round-trip is exercised through the real
``subprocess.run`` against a PATH-installed ``ssh`` shim (the shared
``subprocess_shim`` fixture) that prints a canned ``SAC_DRIFT`` marker
— the same shape a real peer's snippet emits. Resilience paths use a
real injected ``runner`` callable (no patching).

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

import subprocess

from scitex_agent_container._drift import DriftState, check_peer_drift
from scitex_agent_container._drift._fleet import check_fleet_drift
from scitex_agent_container._state.host_config import PeerSpec


def _peers(*names: str) -> dict[str, PeerSpec]:
    return {n: PeerSpec(name=n, ssh=f"user@{n}") for n in names}


# ---------------------------------------------------------------------------
# check_peer_drift via the real ssh shim
# ---------------------------------------------------------------------------


def test_current_marker_parses_to_current(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT current 0 0 origin/develop\n")
    # Act
    row = check_peer_drift("mba", _peers("mba"))
    # Assert
    assert row.status.state is DriftState.CURRENT


def test_behind_marker_parses_behind_count(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT behind 0 4 origin/develop\n")
    # Act
    row = check_peer_drift("nas", _peers("nas"))
    # Assert
    assert row.status.behind == 4


def test_diverged_marker_parses_both_counts(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT diverged 2 5 origin/develop\n")
    # Act
    row = check_peer_drift("spartan", _peers("spartan"))
    # Assert
    assert (row.status.ahead, row.status.behind) == (2, 5)


def test_marker_after_motd_noise_is_still_parsed(subprocess_shim):
    # Arrange — login shells print motd before our marker line.
    subprocess_shim.install(
        "ssh",
        stdout="Welcome to spartan\nLast login: ...\nSAC_DRIFT ahead 1 0 origin/develop\n",
    )
    # Act
    row = check_peer_drift("spartan", _peers("spartan"))
    # Assert
    assert row.status.state is DriftState.AHEAD


def test_not_a_repo_marker_carries_detail(subprocess_shim):
    # Arrange
    subprocess_shim.install(
        "ssh", stdout="SAC_DRIFT not-a-repo 0 0 - (spec source not in a git repo)\n"
    )
    # Act
    row = check_peer_drift("mba", _peers("mba"))
    # Assert
    assert row.status.state is DriftState.NOT_A_REPO


def test_missing_marker_maps_to_unreachable(subprocess_shim):
    # Arrange — ssh succeeds but prints nothing recognizable.
    subprocess_shim.install("ssh", stdout="garbage output, no marker\n")
    # Act
    row = check_peer_drift("mba", _peers("mba"))
    # Assert
    assert row.status.state is DriftState.UNREACHABLE


def test_ssh_nonzero_exit_without_marker_is_unreachable(subprocess_shim):
    # Arrange — connection refused: nonzero exit, stderr only.
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused\n")
    # Act
    row = check_peer_drift("nas", _peers("nas"))
    # Assert
    assert row.status.state is DriftState.UNREACHABLE


def test_undefined_peer_maps_to_unreachable():
    # Arrange — 'ghost' not in the peers map.
    # Act
    row = check_peer_drift("ghost", _peers("mba"))
    # Assert
    assert row.status.state is DriftState.UNREACHABLE


def test_peer_drift_records_host_name(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT current 0 0 origin/develop\n")
    # Act
    row = check_peer_drift("mba", _peers("mba"))
    # Assert
    assert row.host == "mba"


# ---------------------------------------------------------------------------
# resilience via an injected real runner (no mocks)
# ---------------------------------------------------------------------------


def test_ssh_timeout_maps_to_unreachable():
    # Arrange — a real callable that raises TimeoutExpired like ssh would.
    def timing_out_runner(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    # Act
    row = check_peer_drift("nas", _peers("nas"), runner=timing_out_runner)
    # Assert
    assert row.status.state is DriftState.UNREACHABLE


def test_one_bad_host_does_not_crash_the_fleet_sweep():
    # Arrange — runner raises for 'nas', returns a marker for 'mba'.
    def selective_runner(argv, *_a, **_kw):
        if any("nas" in tok for tok in argv):
            raise OSError("spawn failed")
        return subprocess.CompletedProcess(
            argv, 0, stdout="SAC_DRIFT current 0 0 origin/develop\n", stderr=""
        )

    peers = _peers("mba", "nas")
    # Act
    rows = check_fleet_drift(peers, runner=selective_runner)
    # Assert — both hosts reported; the bad one degrades, no exception.
    assert {r.host: r.status.state for r in rows} == {
        "mba": DriftState.CURRENT,
        "nas": DriftState.UNREACHABLE,
    }


def test_fleet_rows_are_sorted_by_host_name():
    # Arrange
    def ok_runner(argv, *_a, **_kw):
        return subprocess.CompletedProcess(
            argv, 0, stdout="SAC_DRIFT current 0 0 origin/develop\n", stderr=""
        )

    peers = _peers("zeta", "alpha", "mid")
    # Act
    rows = check_fleet_drift(peers, runner=ok_runner)
    # Assert
    assert [r.host for r in rows] == ["alpha", "mid", "zeta"]


def test_empty_peers_map_yields_no_rows():
    # Arrange
    # Act
    rows = check_fleet_drift({})
    # Assert
    assert rows == []


def test_remote_snippet_is_passed_through_ssh(subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT current 0 0 origin/develop\n")
    # Act
    check_peer_drift("mba", _peers("mba"))
    argv = subprocess_shim.argv_for("ssh")
    # Assert — the remote command runs the drift snippet via sh -c.
    assert "SAC_DRIFT" in " ".join(argv)
