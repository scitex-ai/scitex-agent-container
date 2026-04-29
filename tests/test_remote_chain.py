"""Tests for spec.remote list-of-hops chain format and location-aware self-skip.

Covers:
  - render_ssh_chain: two-hop, three-hop, empty, single-hop
  - skip_local_hops: full match, partial match, no match
  - parse_remote: list, str (legacy), dict (legacy)
  - SSHRemote._ssh_base: chain format generates -J flag
  - SlurmTenantRuntime: tmux goes via ssh -J <chain>, not srun
"""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, RemoteSpec, SlurmSpec
from scitex_agent_container.config._parsers import parse_remote
from scitex_agent_container.runtimes._ssh_chain import (
    render_ssh_chain,
    skip_local_hops,
)
from scitex_agent_container.runtimes.slurm_tenant import SlurmTenantRuntime

# ---------------------------------------------------------------------------
# render_ssh_chain
# ---------------------------------------------------------------------------


class TestRenderSshChain:
    def test_remote_list_two_hops_renders_dash_J(self):
        result = render_ssh_chain(["spartan", "spartan-bm149"])
        assert result == ["-J", "spartan", "spartan-bm149"]

    def test_remote_list_three_hops_chains_with_comma(self):
        result = render_ssh_chain(["bastion", "jump-internal", "target"])
        assert result == ["-J", "bastion,jump-internal", "target"]

    def test_single_hop_no_dash_J(self):
        result = render_ssh_chain(["myhost"])
        assert result == ["myhost"]

    def test_empty_hops_returns_empty(self):
        assert render_ssh_chain([]) == []

    def test_remote_legacy_single_string_unchanged(self):
        # A single-string remote parsed via parse_remote lands as one hop.
        spec = {"remote": "spartan"}
        remote = parse_remote(spec)
        assert remote.hops == ["spartan"]
        chain = render_ssh_chain(remote.hops)
        assert chain == ["spartan"]  # no -J, just the host


# ---------------------------------------------------------------------------
# skip_local_hops (location-aware self-skip)
# ---------------------------------------------------------------------------


class TestSkipLocalHops:
    def test_remote_chain_runs_locally_when_full_match(self):
        with patch(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            side_effect=lambda h: True,
        ):
            remaining = skip_local_hops(["spartan", "spartan-bm149"])
        assert remaining == []

    def test_remote_chain_partial_skip_renders_ssh_without_J(self):
        # First hop matches local; second does not → single-hop result
        with patch(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            side_effect=lambda h: h == "spartan",
        ):
            remaining = skip_local_hops(["spartan", "spartan-bm149"])
        assert remaining == ["spartan-bm149"]
        # Single remaining hop → no -J
        chain = render_ssh_chain(remaining)
        assert "-J" not in chain
        assert chain == ["spartan-bm149"]

    def test_remote_chain_no_match_renders_full_dash_J(self):
        with patch(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            side_effect=lambda h: False,
        ):
            remaining = skip_local_hops(["spartan", "spartan-bm149"])
        assert remaining == ["spartan", "spartan-bm149"]
        chain = render_ssh_chain(remaining)
        assert chain == ["-J", "spartan", "spartan-bm149"]


# ---------------------------------------------------------------------------
# parse_remote — list and str formats
# ---------------------------------------------------------------------------


class TestParseRemote:
    def test_list_two_hops(self):
        remote = parse_remote({"remote": ["spartan", "spartan-bm149"]})
        assert remote.hops == ["spartan", "spartan-bm149"]
        assert remote.is_remote is True

    def test_list_single_hop(self):
        remote = parse_remote({"remote": ["myhost"]})
        assert remote.hops == ["myhost"]

    def test_str_single_host_becomes_one_hop(self):
        remote = parse_remote({"remote": "spartan"})
        assert remote.hops == ["spartan"]
        assert remote.is_remote is True

    def test_dict_legacy_format_unchanged(self):
        remote = parse_remote({"remote": {"host": "spartan", "user": "yw"}})
        assert remote.host == "spartan"
        assert remote.user == "yw"
        assert remote.hops == []
        assert remote.is_remote is True

    def test_empty_remote_not_remote(self):
        remote = parse_remote({})
        assert remote.is_remote is False


# ---------------------------------------------------------------------------
# SSHRemote._ssh_base uses -J when hops are set
# ---------------------------------------------------------------------------


class TestSSHBaseChain:
    def test_two_hop_chain_includes_dash_J(self):
        from scitex_agent_container.runtimes.ssh_remote import SSHRemote

        config = AgentConfig(
            name="test",
            remote=RemoteSpec(hops=["spartan", "spartan-bm149"]),
        )
        with patch(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            return_value=False,
        ):
            cmd = SSHRemote._ssh_base(config)
        assert "-J" in cmd
        assert "spartan" in cmd
        assert "spartan-bm149" in cmd

    def test_single_hop_no_dash_J(self):
        from scitex_agent_container.runtimes.ssh_remote import SSHRemote

        config = AgentConfig(
            name="test",
            remote=RemoteSpec(hops=["spartan"]),
        )
        with patch(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            return_value=False,
        ):
            cmd = SSHRemote._ssh_base(config)
        assert "-J" not in cmd
        assert "spartan" in cmd


# ---------------------------------------------------------------------------
# SlurmTenantRuntime: tmux goes via ssh -J chain, not srun
# ---------------------------------------------------------------------------


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def fake_reservation():
    res = MagicMock()
    res.id = "spartan-dev-pool"
    res.job_id = "42"
    res.node = "spartan-bm149"
    res.extras = {"tmux_server": "sac"}
    res.exec.return_value = _proc(stdout="")
    return res


@pytest.fixture
def fake_scitex_hpc(monkeypatch, fake_reservation):
    fake_module = types.SimpleNamespace()

    class FakeReservation:
        @staticmethod
        def get(name, host=None):
            return fake_reservation

    fake_module.Reservation = FakeReservation
    monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
    return fake_reservation


def _cfg_with_chain(hops=None):
    return AgentConfig(
        name="dev-helper",
        runtime="slurm-tenant",
        model="sonnet",
        claude=ClaudeSpec(flags=["--dangerously-skip-permissions"]),
        slurm=SlurmSpec(reservation="dev-pool"),
        remote=RemoteSpec(hops=hops or ["spartan", "spartan-bm149"]),
    )


class TestSlurmTenantRuntimeSSHChain:
    def test_tmux_command_goes_via_ssh_dash_J_not_srun(
        self, fake_scitex_hpc, monkeypatch
    ):
        """When spec.remote.hops is set, tmux cmd is sent via ssh -J, not res.exec."""
        monkeypatch.setattr(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            lambda h: False,
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout="NONE\n")
            rt = SlurmTenantRuntime()
            rt.start(_cfg_with_chain(["spartan", "spartan-bm149"]))

        # subprocess.run should have been called (SSH path)
        assert mock_run.called
        # The call must include ssh -J spartan spartan-bm149
        all_calls = [str(c) for c in mock_run.call_args_list]
        assert any("-J" in c for c in all_calls)
        # res.exec (srun path) must NOT have been called
        fake_scitex_hpc.exec.assert_not_called()

    def test_tmux_command_local_when_full_match(self, fake_scitex_hpc, monkeypatch):
        """When all hops match local, tmux runs directly (shell=True, no ssh)."""
        monkeypatch.setattr(
            "scitex_agent_container.runtimes._ssh_chain.is_local_host",
            lambda h: True,
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout="NONE\n")
            rt = SlurmTenantRuntime()
            rt.start(_cfg_with_chain(["spartan", "spartan-bm149"]))

        assert mock_run.called
        # Local run uses shell=True
        first_call = mock_run.call_args_list[0]
        assert first_call.kwargs.get("shell") is True
