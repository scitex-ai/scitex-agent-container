"""``sac agents check`` must refuse a spec whose ``host:`` routes nowhere.

THE BUG THESE PIN. Until 2026-09-05 the preflight validated the container
backend, python, bind-path conventions and ``raw_args``, and never looked at
``spec.host``. Two live specs pinned ``scitex-02`` / ``scitex-01`` -- names
retired on 2026-08-12 when the peer table was re-keyed to
``scitex-compute-0N``. Every runtime path refused them loudly::

    sac agents start scitex-orochi
        -> spec.host is neither this machine nor a registered peer
    agent_spawn scitex-orochi
        -> ssh: Could not resolve hostname scitex-02 (rc=255)

while the preflight answered ``exit 0, "Ready to deploy."``. So an agent that
could not be launched at all read as one that simply had not been, for two
months, and another agent nearly closed its blocked card as "orochi did not
respond" rather than "orochi could not be started".

THE PEER TABLE IS SUPPLIED PER TEST, on purpose. Under pytest the config
cascade resolves to a temp root with no ``config.yaml``, so ``peers`` is empty
and the check degrades to a WARN -- correctly, since an empty registry cannot
convict a name. Every FAIL test therefore plants a real ``config.yaml`` and
points ``$SCITEX_AGENT_CONTAINER_CONFIG`` at it, and one test pins the
empty-registry degrade itself so that behaviour cannot silently become a FAIL.

No mocks (PA-306), consistent with ``test_build_cmds``: real YAML on disk, real
``load_config``, real routing. The network-purity test plants a real fake
``ssh`` on ``PATH`` and asserts production code never invoked it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import check

from tests.scitex_agent_container._helpers.explicit_spec import (
    explicitize_yaml as _explicitize_yaml,
)

# A name no fleet will ever register and no machine will ever answer to.
_UNROUTABLE = "no-such-host-eb4f1c9a"

_PEER_CONFIG = """\
peers:
  scitex-compute-01:
    ssh: scitex-compute-01
  scitex-compute-02:
    ssh: scitex-compute-02
"""


def _spec_body(host: str) -> str:
    return _explicitize_yaml(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  host: {host}\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
    )


def _write(tmp_path: Path, host: str) -> Path:
    spec = tmp_path / "spec.yaml"
    spec.write_text(_spec_body(host))
    return spec


def _register_peers(tmp_path: Path, monkeypatch) -> None:
    """Give this test a real peer registry to judge the pin against."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_PEER_CONFIG)
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))


def test_check_with_unroutable_host_exits_one(tmp_path, monkeypatch):
    # Arrange
    _register_peers(tmp_path, monkeypatch)
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_with_unroutable_host_never_says_ready_to_deploy(
    tmp_path, monkeypatch
):
    # Arrange -- the regression this whole module exists for.
    _register_peers(tmp_path, monkeypatch)
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "Ready to deploy" not in result.output


def test_check_with_unroutable_host_names_the_offending_host(
    tmp_path, monkeypatch
):
    # Arrange
    _register_peers(tmp_path, monkeypatch)
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert _UNROUTABLE in result.output


def test_check_with_unroutable_host_lists_the_registered_peers(
    tmp_path, monkeypatch
):
    # Arrange -- an error must say what WOULD have been accepted.
    _register_peers(tmp_path, monkeypatch)
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "scitex-compute-01" in result.output


def test_check_with_registered_peer_host_reports_ready_to_deploy(
    tmp_path, monkeypatch
):
    # Arrange -- over-rejection control: a real peer must stay green.
    _register_peers(tmp_path, monkeypatch)
    spec = _write(tmp_path, "scitex-compute-02")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "Ready to deploy" in result.output


def test_check_with_no_peers_registered_degrades_to_warn(tmp_path):
    # Arrange -- an EMPTY registry is absence of evidence, not a bad pin.
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_check_with_no_peers_registered_says_why_it_could_not_judge(tmp_path):
    # Arrange
    spec = _write(tmp_path, _UNROUTABLE)
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "no peers" in result.output


def test_check_with_local_host_still_reports_ready_to_deploy(tmp_path):
    # Arrange -- this machine, named explicitly, is always routable.
    spec = _write(tmp_path, "${HOSTNAME}")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "Ready to deploy" in result.output


def test_check_with_local_host_reports_the_host_line_ok(tmp_path):
    # Arrange
    spec = _write(tmp_path, "${HOSTNAME}")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "this machine" in result.output


def test_check_never_invokes_ssh(tmp_path, monkeypatch):
    # Arrange -- a REAL fake ssh on PATH; if the preflight probes, it runs.
    _register_peers(tmp_path, monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sentinel = tmp_path / "ssh-was-called"
    fake = bindir / "ssh"
    fake.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bindir), os.environ.get("PATH", "")])
    )
    spec = _write(tmp_path, "scitex-compute-02")
    runner = CliRunner()
    # Act
    runner.invoke(check, [str(spec)])
    # Assert -- reachability belongs to `sac host probe`, not to this command.
    assert not sentinel.exists()
