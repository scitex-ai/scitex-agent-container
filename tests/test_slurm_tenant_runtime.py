"""Tests for SlurmTenantRuntime — agents as tenants of an existing reservation.

Mocks scitex_hpc.Reservation since the runtime never directly invokes
subprocess; all SLURM interaction is mediated through the Reservation
object.
"""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

from scitex_agent_container.config import AgentConfig, ClaudeSpec, SlurmSpec
from scitex_agent_container.lifecycle import _get_runtime
from scitex_agent_container.runtimes.slurm_tenant import SlurmTenantRuntime

# ---------------------------------------------------------------------------
# Fixtures
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
    res.node = "spartan-bm022"
    # Phase 4: reservation must be booked with tmux_server set so the
    # tmux socket name is discoverable. Mirror the production shape.
    res.extras = {"tmux_server": "sac"}
    res.exec.return_value = _proc(stdout="")
    return res


@pytest.fixture
def fake_scitex_hpc(monkeypatch, fake_reservation):
    """Inject a fake scitex_hpc.Reservation that returns our mock."""
    fake_module = types.SimpleNamespace()

    class FakeReservation:
        @staticmethod
        def get(name, host=None):
            return fake_reservation

    fake_module.Reservation = FakeReservation
    monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
    return fake_reservation


def _cfg(name="dev-helper", reservation="dev-pool", flags=None, model="sonnet"):
    return AgentConfig(
        name=name,
        runtime="slurm-tenant",
        model=model,
        claude=ClaudeSpec(flags=flags or ["--dangerously-skip-permissions"]),
        slurm=SlurmSpec(reservation=reservation),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRuntimeRegistration:
    def test_lifecycle_dispatches_to_slurm_tenant_runtime(self):
        rt = _get_runtime(_cfg())
        assert isinstance(rt, SlurmTenantRuntime)

    def test_unknown_runtime_still_raises(self):
        with pytest.raises(ValueError, match="Unsupported runtime"):
            _get_runtime(AgentConfig(name="x", runtime="bogus"))


# ---------------------------------------------------------------------------
# Reservation lookup
# ---------------------------------------------------------------------------


class TestReservationResolution:
    def test_missing_scitex_hpc_raises_clear_error(self, monkeypatch):
        # Ensure scitex_hpc is NOT in sys.modules
        monkeypatch.setitem(sys.modules, "scitex_hpc", None)
        rt = SlurmTenantRuntime()
        with pytest.raises(RuntimeError, match="requires scitex-hpc"):
            rt.start(_cfg())

    def test_empty_reservation_field_raises(self, fake_scitex_hpc):
        rt = SlurmTenantRuntime()
        with pytest.raises(RuntimeError, match="spec.slurm.reservation"):
            rt.start(_cfg(reservation=""))

    def test_missing_reservation_raises_with_book_hint(self, monkeypatch):
        fake_module = types.SimpleNamespace()
        fake_module.Reservation = type(
            "R",
            (),
            {
                "get": staticmethod(lambda name, host=None: None),
            },
        )
        monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
        rt = SlurmTenantRuntime()
        with pytest.raises(RuntimeError, match="not found"):
            rt.start(_cfg(reservation="ghost"))

    def test_reservation_with_no_jobid_refreshes_then_raises(self, monkeypatch):
        fake_res = MagicMock()
        fake_res.job_id = ""
        fake_res.refresh = MagicMock()
        fake_module = types.SimpleNamespace()
        fake_module.Reservation = type(
            "R",
            (),
            {
                "get": staticmethod(lambda name, host=None: fake_res),
            },
        )
        monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
        rt = SlurmTenantRuntime()
        with pytest.raises(RuntimeError, match="no live job_id"):
            rt.start(_cfg())
        fake_res.refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_runs_tmux_new_session_in_reservation(self, fake_scitex_hpc):
        # No existing session
        fake_scitex_hpc.exec.side_effect = [
            _proc(stdout="NONE\n"),  # has-session check
            _proc(stdout=""),  # tmux new-session
        ]
        rt = SlurmTenantRuntime()
        rt.start(_cfg(name="dev-helper"))

        calls = [c.args[0] for c in fake_scitex_hpc.exec.call_args_list]
        assert "tmux -L sac has-session -t" in calls[0]
        assert "tmux -L sac new-session -d -s" in calls[1]
        assert "sac-dev-helper" in calls[1]
        # Claude command must be embedded
        assert "claude" in calls[1]
        assert "--dangerously-skip-permissions" in calls[1]

    def test_start_skips_when_session_already_exists(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.return_value = _proc(stdout="HAS\n")
        rt = SlurmTenantRuntime()
        # Should not raise; should not run tmux new
        assert rt.start(_cfg()) is True
        # Only the has-session check should have run
        assert fake_scitex_hpc.exec.call_count == 1

    def test_start_force_kills_old_session_first(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.return_value = _proc()
        rt = SlurmTenantRuntime()
        rt.start(_cfg(), force=True)
        # First call should be the kill-session
        first = fake_scitex_hpc.exec.call_args_list[0].args[0]
        assert "tmux -L sac kill-session" in first

    def test_start_propagates_model_flag(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.side_effect = [
            _proc(stdout="NONE"),
            _proc(),
        ]
        rt = SlurmTenantRuntime()
        rt.start(_cfg(model="opus[1m]"))
        new_session_call = fake_scitex_hpc.exec.call_args_list[1].args[0]
        assert "--model" in new_session_call
        assert "opus[1m]" in new_session_call

    def test_start_raises_on_tmux_failure(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.side_effect = [
            _proc(stdout="NONE"),
            _proc(returncode=1, stderr="tmux: command not found"),
        ]
        rt = SlurmTenantRuntime()
        with pytest.raises(RuntimeError, match="tmux new-session failed"):
            rt.start(_cfg())


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_kills_tmux_but_not_reservation(self, fake_scitex_hpc):
        rt = SlurmTenantRuntime()
        assert rt.stop(_cfg(name="helper")) is True
        cmd = fake_scitex_hpc.exec.call_args.args[0]
        assert "tmux -L sac kill-session" in cmd
        assert "sac-helper" in cmd
        # CRITICAL: must NEVER call scancel or release on the reservation
        fake_scitex_hpc.release.assert_not_called()

    def test_stop_returns_false_when_reservation_unavailable(self, monkeypatch):
        fake_module = types.SimpleNamespace()
        fake_module.Reservation = type(
            "R",
            (),
            {
                "get": staticmethod(lambda name, host=None: None),
            },
        )
        monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
        rt = SlurmTenantRuntime()
        assert rt.stop(_cfg(reservation="ghost")) is False


# ---------------------------------------------------------------------------
# is_running
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_is_running_true_when_session_exists(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.return_value = _proc(stdout="HAS\n")
        assert SlurmTenantRuntime().is_running(_cfg()) is True

    def test_is_running_false_when_session_missing(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.return_value = _proc(stdout="NONE\n")
        assert SlurmTenantRuntime().is_running(_cfg()) is False

    def test_is_running_false_when_reservation_unavailable(self, monkeypatch):
        fake_module = types.SimpleNamespace()
        fake_module.Reservation = type(
            "R",
            (),
            {
                "get": staticmethod(lambda name, host=None: None),
            },
        )
        monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
        assert SlurmTenantRuntime().is_running(_cfg()) is False


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_logs_uses_tmux_capture_pane(self, fake_scitex_hpc):
        fake_scitex_hpc.exec.return_value = _proc(stdout="hello world")
        out = SlurmTenantRuntime().logs(_cfg(), lines=10)
        assert out == "hello world"
        cmd = fake_scitex_hpc.exec.call_args.args[0]
        assert "tmux -L sac capture-pane -p -t" in cmd
        assert "-S -10" in cmd

    def test_logs_returns_message_when_reservation_unavailable(self, monkeypatch):
        fake_module = types.SimpleNamespace()
        fake_module.Reservation = type(
            "R",
            (),
            {
                "get": staticmethod(lambda name, host=None: None),
            },
        )
        monkeypatch.setitem(sys.modules, "scitex_hpc", fake_module)
        out = SlurmTenantRuntime().logs(_cfg())
        assert "reservation unavailable" in out


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_slurmspec_reservation_field_default_empty(self):
        spec = SlurmSpec()
        assert spec.reservation == ""

    def test_slurmspec_reservation_field_settable(self):
        spec = SlurmSpec(reservation="dev-pool")
        assert spec.reservation == "dev-pool"

    def test_yaml_parse_round_trip(self, tmp_path):
        from scitex_agent_container.config import load_config

        agent_dir = tmp_path / "tenant-agent"
        agent_dir.mkdir()
        (agent_dir / "tenant-agent.yaml").write_text(
            """
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: slurm-tenant
  slurm:
    reservation: dev-pool
  claude:
    flags: [--dangerously-skip-permissions]
"""
        )
        cfg = load_config(agent_dir / "tenant-agent.yaml")
        assert cfg.runtime == "slurm-tenant"
        assert cfg.slurm.reservation == "dev-pool"


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


class TestAttach:
    def test_attach_invokes_reservation_attach_with_tmux_command(
        self, fake_scitex_hpc
    ):
        fake_scitex_hpc.attach.return_value = 0
        rt = SlurmTenantRuntime()
        rc = rt.attach(_cfg(name="dev-helper"))
        assert rc == 0
        # Reservation.attach should have been called with cmd that includes
        # tmux -L sac attach -t sac-dev-helper
        call = fake_scitex_hpc.attach.call_args
        cmd = call.kwargs.get("cmd") or (call.args[0] if call.args else "")
        assert "tmux -L sac attach -t" in cmd
        assert "sac-dev-helper" in cmd
        assert call.kwargs.get("pty") is True


# ---------------------------------------------------------------------------
# Flag splitting + workspace bootstrap
# ---------------------------------------------------------------------------


class TestBuildClaudeCommand:
    """``_build_claude_command`` must shlex.split each flag entry so YAML
    list items like ``"--dangerously-load-development-channels server:..."``
    end up as two argv tokens, not one space-bundled token that claude
    rejects on parse.
    """

    def test_space_bundled_flag_is_split(self):
        rt = SlurmTenantRuntime()
        cfg = _cfg(
            flags=[
                "--dangerously-skip-permissions",
                "--dangerously-load-development-channels server:scitex-orochi",
            ]
        )
        cmd = rt._build_claude_command(cfg)
        # Both halves of the bundled flag should appear as separate argv
        # tokens in the rendered command (shlex.join keeps them so).
        assert "--dangerously-load-development-channels" in cmd
        assert "server:scitex-orochi" in cmd
        # The bundle should NOT appear quoted as a single token:
        # shlex.join('a b') -> "'a b'", which is the bug we're fixing.
        assert "'--dangerously-load-development-channels server:scitex-orochi'" not in cmd

    def test_already_split_flag_pair_stays_intact(self):
        rt = SlurmTenantRuntime()
        cfg = _cfg(flags=["--add-dir", "/path/with spaces"])
        cmd = rt._build_claude_command(cfg)
        assert "--add-dir" in cmd
        # shlex.join quotes the path with spaces — that's the desired form.
        assert "'/path/with spaces'" in cmd


class TestStartProvisionsWorkspace:
    """``start`` must lay down the workspace files (.mcp.json, CLAUDE.md,
    settings.json, src_*) before launching tmux, otherwise the
    compute-side ``claude`` opens with cwd=$HOME and never finds the
    workspace's MCP wiring. ZOO1 (deterministic > agentic): the
    workspace setup is the same set of helpers ClaudeCodeRuntime uses.
    """

    def test_start_calls_workspace_setup_helpers(self, fake_scitex_hpc, monkeypatch):
        from scitex_agent_container.runtimes import slurm_tenant as st_mod

        fake_scitex_hpc.exec.side_effect = [_proc(stdout="NONE"), _proc()]
        called: dict[str, str] = {}

        def _fake_setup_mcp(cfg, workdir, *a, **kw):
            called["mcp"] = workdir

        def _fake_setup_settings(cfg, workdir, *a, **kw):
            called["settings"] = workdir

        def _fake_setup_claude_md(cfg, workdir, *a, **kw):
            called["claude_md"] = workdir

        def _fake_has_src_files(cfg):
            # Force the legacy (non-v2) branch so setup_claude_md is the
            # CLAUDE.md provider rather than deploy_src_claude_md.
            return False

        monkeypatch.setattr(st_mod, "setup_mcp_config", _fake_setup_mcp)
        monkeypatch.setattr(st_mod, "setup_settings_json", _fake_setup_settings)
        monkeypatch.setattr(st_mod, "setup_claude_md", _fake_setup_claude_md)
        monkeypatch.setattr(st_mod, "_has_src_files", _fake_has_src_files)

        cfg = _cfg(name="dev-helper")
        SlurmTenantRuntime().start(cfg)

        # All three helpers must have been called with the same workdir.
        assert called.keys() == {"mcp", "settings", "claude_md"}
        assert called["mcp"] == cfg.expanded_workdir
        assert called["settings"] == cfg.expanded_workdir
        assert called["claude_md"] == cfg.expanded_workdir

    def test_start_cds_into_workspace_before_claude(
        self, fake_scitex_hpc, monkeypatch
    ):
        from scitex_agent_container.runtimes import slurm_tenant as st_mod

        # Stub setup helpers so the test stays in-memory.
        for name in (
            "setup_mcp_config",
            "setup_settings_json",
            "setup_claude_md",
            "deploy_src_claude_md",
            "deploy_src_mcp_json",
            "deploy_src_env",
        ):
            monkeypatch.setattr(st_mod, name, lambda *a, **kw: None)
        monkeypatch.setattr(st_mod, "_has_src_files", lambda cfg: False)

        fake_scitex_hpc.exec.side_effect = [_proc(stdout="NONE"), _proc()]
        cfg = _cfg(name="dev-helper")
        SlurmTenantRuntime().start(cfg)

        new_session_call = fake_scitex_hpc.exec.call_args_list[1].args[0]
        assert "cd " in new_session_call
        assert cfg.expanded_workdir in new_session_call
        assert "exec claude" in new_session_call
