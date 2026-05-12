"""Tests for sac check-priority command (priority_cmds.py)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.priority_cmds import _priority_report, _probe_ssh

# ---------------------------------------------------------------------------
# _probe_ssh unit tests
# ---------------------------------------------------------------------------


def test_probe_ssh_returns_false_for_nonexistent_host():
    """An unknown host must return False (not raise)."""
    assert _probe_ssh("this-host-does-not-exist-xyz.invalid") is False


def test_probe_ssh_returns_false_on_timeout(monkeypatch):
    """Timeout exception must return False gracefully."""
    import subprocess

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired("ssh", 3)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _probe_ssh("any-host") is False


# ---------------------------------------------------------------------------
# _priority_report unit tests
# ---------------------------------------------------------------------------


import yaml as _yaml


def _write_agent_yaml(tmp_path: Path, host_value) -> str:
    """Write a minimal v3 YAML and return the path string."""
    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir(exist_ok=True)
    yaml_path = agent_dir / "test-agent.yaml"

    spec: dict = {"runtime": "apptainer"}
    if isinstance(host_value, list):
        spec["host"] = host_value
    elif host_value:
        spec["host"] = host_value

    data = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {},
        "spec": spec,
    }
    yaml_path.write_text(_yaml.safe_dump(data))
    return str(yaml_path)


def test_report_single_host_preferred(tmp_path):
    """When current host IS the preferred host, should_yield is False."""
    path = _write_agent_yaml(tmp_path, "spartan")
    report = _priority_report(path, "spartan")
    assert report["should_yield"] is False
    assert "already on highest" in report["reason"]


def test_report_no_host_preference(tmp_path):
    """When no host is set, should_yield is False (run anywhere)."""
    path = _write_agent_yaml(tmp_path, None)
    report = _priority_report(path, "nas")
    assert report["should_yield"] is False


def test_report_fallback_host_all_higher_unreachable(tmp_path, monkeypatch):
    """Fallback host with no higher host reachable → should_yield False."""
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: False,
    )
    path = _write_agent_yaml(tmp_path, ["spartan", "nas", "mba"])
    report = _priority_report(path, "nas")
    assert report["should_yield"] is False
    assert report["current_rank"] == 2
    assert "spartan" in report["unreachable_higher_hosts"]


def test_report_fallback_host_higher_reachable(tmp_path, monkeypatch):
    """Fallback host with a reachable higher host → should_yield True."""

    def fake_probe(h: str) -> bool:
        return h == "spartan"

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        fake_probe,
    )
    path = _write_agent_yaml(tmp_path, ["spartan", "nas", "mba"])
    report = _priority_report(path, "nas")
    assert report["should_yield"] is True
    assert "spartan" in report["reachable_higher_hosts"]


def test_report_host_not_in_chain(tmp_path, monkeypatch):
    """When current host is not in the chain at all, should_yield is False (not running here legitimately)."""
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: True,
    )
    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    report = _priority_report(path, "mba")
    assert report["should_yield"] is False
    assert "not in the priority chain" in report["reason"]


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_cli_json_output_stay(tmp_path, monkeypatch):
    """Standalone priority-check command: should_yield False exits 0."""
    from scitex_agent_container.cli_pkg.priority_cmds import priority_check

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: False,
    )
    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    runner = CliRunner()
    result = runner.invoke(
        priority_check, [path, "--current-host", "spartan", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["should_yield"] is False


def test_cli_json_output_yield(tmp_path, monkeypatch):
    """Standalone priority-check command: should_yield True exits 1."""
    from scitex_agent_container.cli_pkg.priority_cmds import priority_check

    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: True,
    )
    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    runner = CliRunner()
    result = runner.invoke(priority_check, [path, "--current-host", "nas", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["should_yield"] is True
    assert "spartan" in data["reachable_higher_hosts"]


def test_cli_missing_config_exits_2(tmp_path):
    """Non-existent config path exits with code 2."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["priority-check", str(tmp_path / "no-such.yaml"), "--current-host", "nas"],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# singleton-reconcile tests
# ---------------------------------------------------------------------------


def test_singleton_reconcile_no_registered_agents(monkeypatch):
    """When registry is empty, reconcile exits 0 and returns empty list."""
    from scitex_agent_container._state.registry import Registry

    monkeypatch.setattr(Registry, "list_all", lambda self: [])
    runner = CliRunner()
    result = runner.invoke(main, ["registry", "reconcile", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_singleton_reconcile_stay_when_on_preferred(monkeypatch, tmp_path):
    """Agent already on highest-priority host → stay, exit 0."""
    from scitex_agent_container._state.registry import Registry

    path = _write_agent_yaml(tmp_path, ["nas", "mba"])
    monkeypatch.setattr(
        Registry,
        "list_all",
        lambda self: [
            {"name": "test-agent", "config": path, "screen": "test-agent"},
        ],
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: False,
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["registry", "reconcile", "--current-host", "nas", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["action"] == "stay"
    assert data[0]["should_yield"] is False


def test_singleton_reconcile_yield_recommended_dryrun(monkeypatch, tmp_path):
    """Higher-priority host reachable → yield-recommended, exit 1 (dry-run)."""
    from scitex_agent_container._state.registry import Registry

    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    monkeypatch.setattr(
        Registry,
        "list_all",
        lambda self: [
            {"name": "test-agent", "config": path, "screen": "test-agent"},
        ],
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: True,  # spartan reachable
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["registry", "reconcile", "--current-host", "nas", "--json"],
    )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data[0]["action"] == "yield-recommended"
    assert data[0]["preferred_host"] == "spartan"


def test_singleton_reconcile_execute_success(monkeypatch, tmp_path):
    """--execute: remote start succeeds and local stop called → exit 0."""
    from scitex_agent_container._state.registry import Registry

    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    monkeypatch.setattr(
        Registry,
        "list_all",
        lambda self: [
            {"name": "test-agent", "config": path, "screen": "test-agent"},
        ],
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: True,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._ssh_start_agent",
        lambda host, name: True,
    )
    stopped = []
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle.lifecycle.agent_stop",
        lambda name: stopped.append(name),
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["registry", "reconcile", "--execute", "--current-host", "nas", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["action"] == "yielded"
    assert "test-agent" in stopped


def test_singleton_reconcile_execute_remote_fail(monkeypatch, tmp_path):
    """--execute: remote start fails → remote-start-failed, exit non-zero."""
    from scitex_agent_container._state.registry import Registry

    path = _write_agent_yaml(tmp_path, ["spartan", "nas"])
    monkeypatch.setattr(
        Registry,
        "list_all",
        lambda self: [
            {"name": "test-agent", "config": path, "screen": "test-agent"},
        ],
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._probe_ssh",
        lambda h: True,
    )
    monkeypatch.setattr(
        "scitex_agent_container.cli_pkg.priority_cmds._ssh_start_agent",
        lambda host, name: False,
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["registry", "reconcile", "--execute", "--current-host", "nas", "--json"],
    )
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data[0]["action"] == "remote-start-failed"
