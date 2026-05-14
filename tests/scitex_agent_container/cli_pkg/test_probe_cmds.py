"""Tests for the ``probe-hub`` CLI command (todo#457).

No-mocks pattern: ``run_and_log`` accepts an injected ``probes``
callable + reads ``$SAC_PROBE_LOG_ROOT`` for the JSONL output dir.
Tests pass real callables and real env values — no monkeypatch on
production internals.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container._network import probe as np
from scitex_agent_container._network.probe import run_and_log
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


# ---------------------------------------------------------------------------
# run_and_log() — the core, tested directly with injected probes.
# ---------------------------------------------------------------------------


def test_run_and_log_writes_summary_to_jsonl(tmp_path: Path):
    # Arrange
    agent = "test-agent"
    # Act
    run_and_log(
        agent,
        hub_host="hub.example",
        hub_url="https://hub.example/",
        root=tmp_path,
        probes=_fake_all_ok,
    )
    # Assert
    assert (tmp_path / f"{agent}.jsonl").is_file()


def test_run_and_log_summary_ok_when_all_probes_pass(tmp_path: Path):
    # Arrange
    # Act
    summary = run_and_log(
        "a", hub_host="x", hub_url="https://x/", root=tmp_path, probes=_fake_all_ok
    )
    # Assert
    assert summary["ok"] is True


def test_run_and_log_summary_fails_when_any_probe_fails(tmp_path: Path):
    # Arrange
    # Act
    summary = run_and_log(
        "a", hub_host="x", hub_url="https://x/", root=tmp_path, probes=_fake_dns_fail
    )
    # Assert
    assert summary["ok"] is False


def test_run_and_log_jsonl_contains_four_probes(tmp_path: Path):
    # Arrange
    agent = "a"
    # Act
    run_and_log(
        agent, hub_host="x", hub_url="https://x/", root=tmp_path, probes=_fake_all_ok
    )
    # Assert
    payload = json.loads((tmp_path / f"{agent}.jsonl").read_text().strip())
    assert len(payload["probes"]) == 4


# ---------------------------------------------------------------------------
# CLI surface — env override for log root, probes default to real.
# Tests use a hub_url with an RFC2606-reserved domain (``hub.example``)
# so the real probes naturally fail without any network.
# ---------------------------------------------------------------------------


class TestProbeNetworkCLI:
    def test_env_override_redirects_log_root(self, tmp_path: Path, env_save_restore):
        # Arrange
        env_save_restore.set("SAC_PROBE_LOG_ROOT", str(tmp_path))
        env_save_restore.set("SAC_HUB_URL", "https://hub.example/")
        # Act
        result = CliRunner().invoke(
            main, ["host", "probe-hub", "--agent", "test-agent", "--quiet"]
        )
        # Assert
        # The probes will fail (RFC2606 host), but the JSONL must still be
        # written to the redirected log root.
        assert (tmp_path / "test-agent.jsonl").is_file(), result.output

    def test_exit_nonzero_flag_propagates_failure(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SAC_PROBE_LOG_ROOT", str(tmp_path))
        env_save_restore.set("SAC_HUB_URL", "https://hub.example/")
        # Act
        result = CliRunner().invoke(
            main,
            [
                "host",
                "probe-hub",
                "--agent",
                "a",
                "--quiet",
                "--exit-nonzero-on-fail",
            ],
        )
        # Assert
        # Probes fail against the RFC2606 example domain, exit code is 1.
        assert result.exit_code == 1

    def test_claude_agent_id_env_used_as_agent_name(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SAC_PROBE_LOG_ROOT", str(tmp_path))
        env_save_restore.set("SAC_HUB_URL", "https://hub.example/")
        env_save_restore.set("CLAUDE_AGENT_ID", "head-ywata-note-win")
        # Act
        CliRunner().invoke(main, ["host", "probe-hub", "--quiet"])
        # Assert
        assert (tmp_path / "head-ywata-note-win.jsonl").is_file()
