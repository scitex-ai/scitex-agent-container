"""CLI tests for ``sac fleet`` — peer-aware orchestration.

Exercises spec discovery, --peer validation, dry-run, rsync invocation,
and ssh-launch fanout. All subprocess calls are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container._state.host_config import (
    Config,
    HostBlock,
    PeerSpec,
)
from scitex_agent_container.cli_pkg import fleet_group as fg
from scitex_agent_container.cli_pkg.fleet_group import (
    _discover_specs,
    fleet_group,
)


@pytest.fixture(autouse=True)
def _home_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


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


# ---------------------------------------------------------------------------
# _discover_specs
# ---------------------------------------------------------------------------


def test_discover_specs_v3_layout(tmp_path: Path) -> None:
    root = _make_spec_dir(tmp_path / "specs", ["a", "b"], style="v3")
    assert _discover_specs(root) == ["a", "b"]


def test_discover_specs_legacy_layout(tmp_path: Path) -> None:
    root = _make_spec_dir(tmp_path / "specs", ["legacy"], style="v2")
    assert _discover_specs(root) == ["legacy"]


def test_discover_specs_ignores_files_and_emptydirs(tmp_path: Path) -> None:
    root = tmp_path / "specs"
    root.mkdir()
    (root / "stray.txt").write_text("")
    (root / "emptydir").mkdir()
    (root / "ok").mkdir()
    (root / "ok" / "ok.yaml").write_text("{}")
    assert _discover_specs(root) == ["ok"]


# ---------------------------------------------------------------------------
# launch — error paths
# ---------------------------------------------------------------------------


def _cfg_with_peer(
    name: str = "spartan", ssh: str = "user@host", via: tuple = ()
) -> Config:
    return Config(
        host=HostBlock(),
        peers={name: PeerSpec(name=name, ssh=ssh, via=via)},
        source_path=Path("/tmp/cfg.yaml"),
    )


def test_launch_rejects_unknown_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer("real"))
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "missing"])
    assert result.exit_code == 2
    assert "not defined" in result.output


def test_launch_errors_when_no_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    empty = tmp_path / "empty"
    empty.mkdir()
    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(empty), "--peer", "spartan"])
    assert result.exit_code == 2
    assert "no specs" in result.output


# ---------------------------------------------------------------------------
# launch — dry-run
# ---------------------------------------------------------------------------


def test_launch_dry_run_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])
    runner = CliRunner()
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "start on spartan: a" in result.output
    assert "start on spartan: b" in result.output


def test_launch_dry_run_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--dry-run", "--json"],
    )
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["plan"]["names"] == ["a"]
    assert body["rows"] == []


def test_launch_dry_run_no_rsync_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a"])
    runner = CliRunner()
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--dry-run", "--no-rsync"],
    )
    assert result.exit_code == 0
    assert "skipped" in result.output


# ---------------------------------------------------------------------------
# launch — real subprocess (mocked) flow
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_launch_runs_rsync_then_ssh_per_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])

    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return _Proc(rc=0, stdout="ok\n")

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(
        fg, "build_ssh_argv", lambda peer, cmd, peers: ["ssh", "host", *cmd]
    )

    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    assert result.exit_code == 0
    # First call: rsync
    assert seen[0][0] == "rsync"
    # Followed by one ssh call per agent
    ssh_calls = [c for c in seen if c[0] == "ssh"]
    assert len(ssh_calls) == 2


def test_launch_rsync_failure_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a"])

    def fake_run(argv, **kw):
        if argv[0] == "rsync":
            return _Proc(rc=23)
        return _Proc(rc=0)

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh"])

    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    assert result.exit_code == 1
    assert "rsync failed" in result.output


def test_launch_failure_exit_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero per-agent ssh exit aggregates to overall exit 1."""
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a", "b"])

    def fake_run(argv, **kw):
        if argv[0] == "rsync":
            return _Proc(rc=0)
        # First ssh ok, second fail
        return _Proc(rc=2, stderr="boom")

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh"])

    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    assert result.exit_code == 1


def test_launch_no_rsync_skips_rsync_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a"])

    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return _Proc(rc=0)

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh", "host"])

    runner = CliRunner()
    result = runner.invoke(
        fleet_group,
        ["launch", str(spec), "--peer", "spartan", "--no-rsync"],
    )
    assert result.exit_code == 0
    assert not any(c[0] == "rsync" for c in seen)


def test_launch_explicit_spec_without_specdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--spec NAME (no SPECDIR) — works with --no-rsync."""
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())

    def fake_run(argv, **kw):
        return _Proc(rc=0, stdout="started")

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh"])

    runner = CliRunner()
    result = runner.invoke(
        fleet_group,
        ["launch", "--peer", "spartan", "--spec", "alpha", "--no-rsync"],
    )
    assert result.exit_code == 0


def test_launch_json_output_includes_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fg, "load", lambda: _cfg_with_peer())
    spec = _make_spec_dir(tmp_path / "specs", ["a"])

    monkeypatch.setattr(fg.subprocess, "run", lambda *a, **kw: _Proc(rc=0, stdout="ok"))
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh"])

    runner = CliRunner()
    result = runner.invoke(
        fleet_group, ["launch", str(spec), "--peer", "spartan", "--json"]
    )
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["plan"]["names"] == ["a"]
    assert body["rows"][0]["exit"] == 0


def test_launch_rsync_uses_jump_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When peer.via is set, rsync gets -e 'ssh -J ...'."""
    cfg = Config(
        host=HostBlock(),
        peers={
            "spartan": PeerSpec(name="spartan", ssh="user@spartan", via=("bastion",)),
            "bastion": PeerSpec(name="bastion", ssh="me@bastion"),
        },
        source_path=Path("/tmp/cfg.yaml"),
    )
    monkeypatch.setattr(fg, "load", lambda: cfg)
    spec = _make_spec_dir(tmp_path / "specs", ["a"])

    captured: list[list[str]] = []

    def fake_run(argv, **kw):
        captured.append(list(argv))
        return _Proc(rc=0)

    monkeypatch.setattr(fg.subprocess, "run", fake_run)
    monkeypatch.setattr(fg, "build_ssh_argv", lambda *a, **kw: ["ssh"])

    runner = CliRunner()
    result = runner.invoke(fleet_group, ["launch", str(spec), "--peer", "spartan"])
    assert result.exit_code == 0
    rsync_cmd = captured[0]
    assert "-e" in rsync_cmd
    e_idx = rsync_cmd.index("-e")
    assert "ssh -J" in rsync_cmd[e_idx + 1]
