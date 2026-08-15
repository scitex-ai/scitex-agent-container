"""Tests for ``sac fleet`` group — peer-aware orchestration.

PA-306 no-mocks rewrite. The previous version relied on
``monkeypatch.setattr`` to swap ``fg.load`` (the config loader),
``fg.subprocess.run`` (the shell-out), and ``fg.build_ssh_argv`` (the
argv builder). Every test was therefore checking what production did
to a mock rather than what it does to reality.

Real-collaborator seams used here:

* ``SCITEX_AGENT_CONTAINER_CONFIG`` env var (already honoured by
  ``host_config._default_config_path``) points production at a real
  YAML file written into ``tmp_path``. The real ``load()`` parses it
  into real ``Config`` / ``PeerSpec`` dataclasses. No monkeypatch on
  ``fg.load``.

* ``subprocess_shim`` (shared helper) installs real-on-disk fake
  binaries for ``rsync`` and ``ssh`` on ``PATH``. Production's real
  ``subprocess.run`` finds them via the real PATH lookup, executes
  them, and the shim records argv per invocation. No monkeypatch on
  ``fg.subprocess.run`` and no monkeypatch on ``fg.build_ssh_argv`` —
  the real ``build_ssh_argv`` runs and emits an argv whose first
  element ``"ssh"`` is resolved by PATH to our shim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.fleet_group import (
    _discover_specs,
    fleet_group,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_spec_dir(root: Path, names: list[str], style: str = "v3") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        d = root / n
        d.mkdir()
        if style == "v3":
            (d / f"{n}.yaml").write_text(
                yaml.safe_dump({"apiVersion": "scitex-agent-container/v3"})
            )
        else:
            (d / "spec.yaml").write_text(
                yaml.safe_dump({"apiVersion": "scitex-agent-container/v2"})
            )
    return root


def _write_config(
    tmp_path: Path,
    env_save_restore,
    peers: dict[str, dict],
) -> Path:
    """Write a real config.yaml and point production at it via env var.

    Real seam: ``host_config._default_config_path`` honours
    ``SCITEX_AGENT_CONTAINER_CONFIG``; setting it makes the real
    ``load()`` parse our file. No monkeypatch.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"peers": peers}))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))
    return cfg_path


@pytest.fixture
def sandbox_home(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` into ``tmp_path`` so any ``Path.home()`` lookup
    stays inside the test sandbox. Real env var mutation; no monkeypatch.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# _discover_specs — pure filesystem function, no collaborators
# ---------------------------------------------------------------------------


def test_discover_specs_returns_v3_layout_names_in_sorted_order(tmp_path: Path) -> None:
    # Arrange
    root = _make_spec_dir(tmp_path / "specs", ["a", "b"], style="v3")
    # Act
    result = _discover_specs(root)
    # Assert
    assert result == ["a", "b"]


def test_discover_specs_returns_legacy_spec_yaml_layout_names(tmp_path: Path) -> None:
    # Arrange
    root = _make_spec_dir(tmp_path / "specs", ["legacy"], style="v2")
    # Act
    result = _discover_specs(root)
    # Assert
    assert result == ["legacy"]


def test_discover_specs_ignores_files_and_emptydirs(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "specs"
    root.mkdir()
    (root / "stray.txt").write_text("")
    (root / "emptydir").mkdir()
    (root / "ok").mkdir()
    (root / "ok" / "ok.yaml").write_text("{}")
    # Act
    result = _discover_specs(root)
    # Assert
    assert result == ["ok"]


# ---------------------------------------------------------------------------
# launch — error paths (no subprocess invocation required)
# ---------------------------------------------------------------------------


def test_launch_rejects_unknown_peer_with_exit_2(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"real": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "missing"])
    # Assert
    assert result.exit_code == 2


def test_launch_rejects_unknown_peer_with_helpful_message(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"real": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "missing"])
    # Assert
    assert "not defined" in result.output


def test_launch_errors_with_exit_2_when_no_specs_found(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    empty = tmp_path / "empty"
    empty.mkdir()
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(empty), "--peer", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_launch_errors_with_no_specs_message_when_specdir_is_empty(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    empty = tmp_path / "empty"
    empty.mkdir()
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(empty), "--peer", "spartan"])
    # Assert
    assert "no specs" in result.output


# ---------------------------------------------------------------------------
# launch — dry-run (no subprocess invocation, real config)
# ---------------------------------------------------------------------------


def test_launch_dry_run_exits_zero(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--dry-run"]
    )
    # Assert
    assert result.exit_code == 0


def test_launch_dry_run_prints_dry_run_marker(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--dry-run"]
    )
    # Assert
    assert "DRY RUN" in result.output


def test_launch_dry_run_lists_each_discovered_agent(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--dry-run"]
    )
    # Assert
    assert (
        "start on spartan: a" in result.output
        and "start on spartan: b" in result.output
    )


def test_launch_dry_run_json_emits_plan_names_in_order(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--dry-run", "--json"],
    )
    body = json.loads(result.stdout)
    # Assert
    assert body["plan"]["names"] == ["a"]


def test_launch_dry_run_json_emits_empty_rows(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--dry-run", "--json"],
    )
    body = json.loads(result.stdout)
    # Assert
    assert body["rows"] == []


def test_launch_dry_run_with_no_rsync_announces_skip(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "u@h"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--dry-run", "--no-rsync"],
    )
    # Assert
    assert "skipped" in result.output


# ---------------------------------------------------------------------------
# launch — real subprocess via PATH-resident shim binaries
# ---------------------------------------------------------------------------


def test_launch_invokes_rsync_once_and_ssh_per_agent(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=0, stdout="ok\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    # Assert
    assert (
        result.exit_code == 0
        and subprocess_shim.call_count("rsync") == 1
        and subprocess_shim.call_count("ssh") == 2
    )


def test_launch_rsync_failure_exits_one(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=23)
    subprocess_shim.install("ssh", exit=0)
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    # Assert
    assert result.exit_code == 1


def test_launch_rsync_failure_reports_rsync_failed(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=23)
    subprocess_shim.install("ssh", exit=0)
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    # Assert
    assert "rsync failed" in result.output


def test_launch_aggregates_per_agent_ssh_failure_into_exit_one(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange — rsync ok, every ssh exits 2.
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=2, stderr="boom")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    # Assert
    assert result.exit_code == 1


def test_launch_no_rsync_flag_skips_rsync_invocation(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=0)
    runner = CliRunner()
    # Act
    runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan", "--no-rsync"])
    # Assert
    assert subprocess_shim.call_count("rsync") == 0


def test_launch_explicit_spec_without_specdir_runs_via_no_rsync(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    subprocess_shim.install("ssh", exit=0, stdout="started")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["launch", "--peer", "spartan", "--spec", "alpha", "--no-rsync"],
    )
    # Assert
    assert result.exit_code == 0


def test_launch_json_output_row_exit_zero_when_ssh_succeeds(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=0, stdout="ok")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--json"]
    )
    body = json.loads(result.stdout)
    # Assert
    assert body["rows"][0]["exit"] == 0


def test_launch_json_output_plan_names_matches_discovered_specs(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, {"spartan": {"ssh": "user@host"}})
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=0)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--json"]
    )
    body = json.loads(result.stdout)
    # Assert
    assert body["plan"]["names"] == ["a"]


def test_launch_rsync_uses_proxy_jump_chain_when_peer_has_via(
    tmp_path: Path, env_save_restore, subprocess_shim
) -> None:
    # Arrange — spartan reached via bastion; real config, real argv build.
    _write_config(
        tmp_path,
        env_save_restore,
        {
            "spartan": {"ssh": "user@spartan", "via": ["bastion"]},
            "bastion": {"ssh": "me@bastion"},
        },
    )
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    subprocess_shim.install("rsync", exit=0)
    subprocess_shim.install("ssh", exit=0)
    runner = CliRunner()
    # Act
    runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    rsync_argv = subprocess_shim.argv_for("rsync") or []
    e_idx = rsync_argv.index("-e") if "-e" in rsync_argv else -1
    # Assert
    assert e_idx >= 0 and "ssh -J" in rsync_argv[e_idx + 1]


# EOF
