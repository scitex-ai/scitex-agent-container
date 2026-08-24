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


# ---------------------------------------------------------------------------
# sac doctor --collisions  (do two SPECS resolve to the same bot token?)
# ---------------------------------------------------------------------------
#
# The OTHER half of the one-token-one-poller invariant, and the half the
# poller check structurally cannot reach: it reads SPECS + the secrets pool,
# so it sees a duplicate split across hosts and sees it before either process
# starts. Measured 2026-08-22, one bot token was held on compute-04 and
# compute-03 at once and the per-host poller probe returned ok on BOTH.
#
# These drive the REAL host spec tree and the REAL pool, so the STATE is
# whatever this machine is actually configured as and must NOT be asserted.
# What IS deterministic -- and what these pin -- is that the check runs,
# reports one of exactly three values, rides along by default, and states its
# own scope. The state TRANSITIONS are measured against synthetic spec trees
# in tests/.../runtimes/test__cct_token_collision.py, where the population is
# controlled.

_COLLISION_STATES = {"ok", "violation", "unknown"}


def test_collisions_json_carries_a_three_valued_state():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--collisions", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["token_collisions"]["state"] in _COLLISION_STATES


def test_collisions_only_omits_the_drift_check():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--collisions", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "local" not in payload


def test_collisions_only_omits_the_poller_check():
    # Arrange -- the two checks are complements, not duplicates, so asking for
    # one must not silently run the other.
    # Act
    result = CliRunner().invoke(doctor, ["--collisions", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "pollers" not in payload


def test_pollers_only_omits_the_collision_check():
    # Arrange -- the same statement from the other side.
    # Act
    result = CliRunner().invoke(doctor, ["--pollers", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "token_collisions" not in payload


def test_collisions_human_output_names_the_check():
    # Arrange
    # Act
    result = CliRunner().invoke(doctor, ["--collisions"])
    # Assert
    assert "spec bot tokens" in result.output


def test_collisions_output_states_how_many_specs_were_examined():
    # Arrange -- a clean count means nothing without its denominator: a peer
    # published a census that was 3-of-10 and read as 3-of-3.
    # Act
    result = CliRunner().invoke(doctor, ["--collisions"])
    # Assert
    assert "spec(s) examined" in result.output


def test_collisions_json_points_at_the_other_half_of_the_invariant():
    # Arrange -- an unstated limit is the same defect as a wrong hint, and
    # neither of these checks alone is a fleet all-clear.
    # Act
    result = CliRunner().invoke(doctor, ["--collisions", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "sac doctor --pollers" in payload["token_collisions"]["scope_note"]


def test_default_doctor_runs_the_collision_check_too(local_drifted_source):
    # Arrange -- a check nobody remembers to run is how a config collision
    # survives a process kill and returns on the next start.
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["token_collisions"]["state"] in _COLLISION_STATES


def test_default_doctor_still_carries_the_poller_verdict(local_drifted_source):
    # Arrange -- adding a check must not displace the one already there.
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["pollers"]["state"] in _POLLER_STATES


# --------------------------------------------------------------------------
# sac doctor --node
#
# The predicate itself is tested in tests/scitex_agent_container/_readiness/.
# What matters HERE is that it is actually WIRED: a host that cannot equip an
# agent has to say so on stdout, in JSON, and in the exit code under --strict.
# A correct predicate nobody runs is the same as no check, which is the exact
# failure this whole feature exists to fix.
#
# Nothing is patched. $SAC_USER_TO_HOME_BASELINE is a REAL production override
# already honoured by the resolver a deploy uses, so the command under test
# runs its true code path and only the directory it inspects varies.
# --------------------------------------------------------------------------

_BASELINE_ENV = "SAC_USER_TO_HOME_BASELINE"


def _servable(tmp_path: Path, name: str) -> dict:
    """A declared server whose command genuinely exists on this machine."""
    exe = tmp_path / f"bin-{name}"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return {"command": str(exe)}


def _baseline_dir(tmp_path: Path, servers: dict) -> Path:
    root = tmp_path / "to_home"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return root


@pytest.fixture
def ready_node(tmp_path: Path, env_save_restore) -> Path:
    """A host whose every declared server can actually be served."""
    root = _baseline_dir(tmp_path, {"cards": _servable(tmp_path, "cards")})
    env_save_restore.set(_BASELINE_ENV, str(root))
    return root


@pytest.fixture
def crippled_node(tmp_path: Path, env_save_restore) -> Path:
    """One working server and one stub — measured on a real host 2026-08-23."""
    root = _baseline_dir(
        tmp_path,
        {
            "cards": _servable(tmp_path, "cards"),
            "telegrammer": {"command": "/usr/bin/true"},
        },
    )
    env_save_restore.set(_BASELINE_ENV, str(root))
    return root


def test_doctor_node_prints_the_tool_count(crippled_node):
    # Arrange -- "how many tools would an agent get here" is the question the
    # operator actually asked, so it belongs on screen, not only in the JSON.
    # Act
    result = CliRunner().invoke(doctor, ["--node"])
    # Assert
    assert "1 MCP server" in result.output


def test_doctor_node_names_the_stub_channel(crippled_node):
    # Arrange -- an agent read this exact stub, correctly inferred "someone
    # disabled my Telegram", and told the operator so. No such decision existed.
    # Act
    result = CliRunner().invoke(doctor, ["--node"])
    # Assert
    assert "telegrammer" in result.output


def test_doctor_node_strict_exits_nonzero_when_crippled(crippled_node):
    # Arrange -- without a non-zero exit this can never gate provisioning, and
    # gating provisioning is the only thing that stops the next node repeating.
    # Act
    result = CliRunner().invoke(doctor, ["--node", "--strict"])
    # Assert
    assert result.exit_code != 0


def test_doctor_node_json_carries_the_verdict(ready_node):
    # Arrange -- a fleet sweep consumes JSON; that is what makes this usable
    # across 122 agents instead of one host at a time.
    # Act
    result = CliRunner().invoke(doctor, ["--node", "--json"])
    # Assert
    assert json.loads(result.stdout)["node"]["verdict"] == "ready"


def test_doctor_node_reports_the_tool_count_in_json(ready_node):
    # Arrange -- the count is the machine-readable form of the answer.
    # Act
    result = CliRunner().invoke(doctor, ["--node", "--json"])
    # Assert
    assert json.loads(result.stdout)["node"]["tool_count"] == 1


def test_doctor_node_runs_only_the_node_check(ready_node):
    # Arrange -- --node is a single-check flag like --pollers. Leaking the
    # others in would make it ssh and read /proc, too slow for a setup script.
    # Act
    result = CliRunner().invoke(doctor, ["--node", "--json"])
    # Assert
    assert list(json.loads(result.stdout)) == ["node"]


def test_default_doctor_carries_the_node_verdict(local_drifted_source):
    # Arrange -- the gap was invisible for days precisely because no default
    # surface reported it, so it must run without anyone asking for it.
    # Act
    result = CliRunner().invoke(doctor, ["--json"])
    # Assert
    assert "verdict" in json.loads(result.stdout)["node"]
