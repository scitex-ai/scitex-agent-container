"""CLI tests for ``sac doctor`` (drift diagnostics).

PA-306: no mocks. ``CliRunner`` drives the real click command; the
fleet ssh round-trip uses the shared ``subprocess_shim`` (a real fake
binary on PATH) and a real tmp_path config.yaml surfaced via the
SCITEX_AGENT_CONTAINER_CONFIG env override.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.doctor_cmds import doctor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def cfg_with_peer(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml (one peer) surfaced via the env override."""
    p = tmp_path / "config.yaml"
    p.write_text("peers:\n  mba: { ssh: user@mba }\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


@pytest.fixture
def empty_cfg(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml with no peers."""
    p = tmp_path / "config.yaml"
    p.write_text("peers: {}\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# sac doctor --fleet
# ---------------------------------------------------------------------------


def test_fleet_table_renders_peer_drift(subprocess_shim, cfg_with_peer):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT behind 0 3 origin/develop\n")
    # Act
    result = CliRunner().invoke(doctor, ["--fleet"])
    # Assert
    assert "3 behind origin/develop" in result.output


def test_fleet_json_lists_each_peer(subprocess_shim, cfg_with_peer):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT current 0 0 origin/develop\n")
    # Act
    result = CliRunner().invoke(doctor, ["--fleet", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["peers"][0]["host"] == "mba"


def test_fleet_empty_config_notes_no_peers(empty_cfg):
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--fleet"])
    # Assert
    assert "no peers configured" in result.output


def test_fleet_strict_exits_nonzero_on_drift(subprocess_shim, cfg_with_peer):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT diverged 2 5 origin/develop\n")
    # Act
    result = CliRunner().invoke(doctor, ["--fleet", "--strict"])
    # Assert
    assert result.exit_code == 1


def test_fleet_strict_exits_zero_when_all_current(subprocess_shim, cfg_with_peer):
    # Arrange
    subprocess_shim.install("ssh", stdout="SAC_DRIFT current 0 0 origin/develop\n")
    # Act
    result = CliRunner().invoke(doctor, ["--fleet", "--strict"])
    # Assert
    assert result.exit_code == 0


def test_fleet_unreachable_peer_does_not_crash(subprocess_shim, cfg_with_peer):
    # Arrange — ssh refused: nonzero exit, no marker.
    subprocess_shim.install("ssh", exit=255, stderr="connect: refused\n")
    # Act
    result = CliRunner().invoke(doctor, ["--fleet"])
    # Assert — reported, not crashed.
    assert result.exit_code == 0 and "unreachable" in result.output


# ---------------------------------------------------------------------------
# sac doctor (local)
# ---------------------------------------------------------------------------


@pytest.fixture
def local_drifted_source(tmp_path: Path, env_save_restore):
    """Point the local probe at a real BEHIND git repo via SCITEX_DIR.

    ``_local_agents_spec_dir()`` resolves ``~/.scitex/agent-container/
    agents`` — relocating ``$SCITEX_DIR`` (which expands ``~``-> via
    HOME isn't enough), so we instead set HOME to a tmp dir whose
    ``.scitex/agent-container/agents`` is the working clone.
    """
    home = tmp_path / "home"
    agents_parent = home / ".scitex" / "agent-container"
    agents_parent.mkdir(parents=True)

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    work = agents_parent / "agents"
    subprocess.run(
        ["git", "clone", str(remote), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-b", "develop")
    (work / "foo").mkdir()
    (work / "foo" / "spec.yaml").write_text("x")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "develop")
    # Make the local clone BEHIND by pushing from a second clone.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "develop")
    (other / "new.txt").write_text("remote")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote work")
    _git(other, "push")

    env_save_restore.set("HOME", str(home))
    # Bust any cross-test fetch cache by relocating the cache root too.
    env_save_restore.set("SCITEX_DIR", str(home / ".scitex"))
    return work


def test_local_check_reports_drift_summary(local_drifted_source):
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, [])
    # Assert
    assert "behind origin/develop" in result.output


def test_local_strict_exits_nonzero_on_drift(local_drifted_source):
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--strict"])
    # Assert
    assert result.exit_code == 1


def test_local_json_carries_state_field(local_drifted_source):
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["local"]["state"] == "behind"


# ---------------------------------------------------------------------------
# sac doctor --pollers  (one live Telegram poller per bot token?)
# ---------------------------------------------------------------------------
#
# These drive the REAL host /proc, so the STATE is whatever this machine is
# actually doing right now and must NOT be asserted. What IS deterministic --
# and what these pin -- is that the check runs, reports one of exactly three
# values, and rides along by default. The state TRANSITIONS are measured
# against real spawned processes in
# tests/.../runtimes/test__cct_poller_singleton.py, where the population is
# controlled.

_POLLER_STATES = {"ok", "violation", "unknown"}


def test_pollers_json_carries_a_three_valued_state():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--pollers", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["pollers"]["state"] in _POLLER_STATES


def test_pollers_only_omits_the_drift_check():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--pollers", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "local" not in payload


def test_pollers_human_output_names_the_check():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--pollers"])
    # Assert
    assert "telegram pollers" in result.output


def test_default_doctor_runs_the_poller_check_too(local_drifted_source):
    # Arrange -- a check nobody remembers to run is how a 409 storm survives
    # for weeks, so the poller verdict must appear without being asked for.
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["pollers"]["state"] in _POLLER_STATES


def test_default_doctor_still_carries_the_drift_verdict(local_drifted_source):
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["local"]["state"] == "behind"
