"""Tests for the ``probe-network`` CLI command (todo#457)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container._network import probe as np
from scitex_agent_container.cli import main


def _fake_all_ok(**_kwargs):
    return [
        np.ProbeResult(name="dns", ok=True, latency_ms=1.0),
        np.ProbeResult(name="gateway", ok=True, latency_ms=1.0),
        np.ProbeResult(name="tcp", ok=True, latency_ms=1.0),
        np.ProbeResult(name="https", ok=True, latency_ms=1.0),
    ]


def _fake_dns_fail(**_kwargs):
    return [
        np.ProbeResult(name="dns", ok=False, latency_ms=1.0, err="gaierror"),
        np.ProbeResult(name="gateway", ok=True, latency_ms=1.0),
        np.ProbeResult(name="tcp", ok=False, latency_ms=1.0, err="gaierror"),
        np.ProbeResult(name="https", ok=False, latency_ms=1.0, err="gaierror"),
    ]


class TestProbeNetworkCLI:
    def test_all_ok_returns_zero(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(np, "run_all_probes", _fake_all_ok)
        monkeypatch.setattr(np, "DEFAULT_LOG_ROOT", tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["network", "probe", "--agent", "test-agent", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        path = tmp_path / "test-agent.jsonl"
        assert path.exists()
        payload = json.loads(path.read_text().strip())
        assert payload["ok"] is True
        assert len(payload["probes"]) == 4

    def test_non_quiet_prints_json(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(np, "run_all_probes", _fake_all_ok)
        monkeypatch.setattr(np, "DEFAULT_LOG_ROOT", tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["network", "probe", "--agent", "a"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True

    def test_exit_nonzero_on_fail(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(np, "run_all_probes", _fake_dns_fail)
        monkeypatch.setattr(np, "DEFAULT_LOG_ROOT", tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "network", "probe",
                "--agent",
                "a",
                "--quiet",
                "--exit-nonzero-on-fail",
            ],
        )
        assert result.exit_code == 1
        path = tmp_path / "a.jsonl"
        assert path.exists()
        payload = json.loads(path.read_text().strip())
        assert payload["ok"] is False

    def test_env_fallback_for_agent(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(np, "run_all_probes", _fake_all_ok)
        monkeypatch.setattr(np, "DEFAULT_LOG_ROOT", tmp_path)
        monkeypatch.setenv("SCITEX_OROCHI_AGENT", "head-ywata-note-win")
        runner = CliRunner()
        result = runner.invoke(main, ["network", "probe", "--quiet"])
        assert result.exit_code == 0
        assert (tmp_path / "head-ywata-note-win.jsonl").exists()
