"""Regression tests for the generic SLURM runtime.

Covers:
  * Hardener strings (set -euo pipefail, set -x, log redirect, exit trap,
    USR1 auto-resubmit trap, persistent hold).
  * Plugin hook contract (pre_submit, pre_agent, walltime_signal,
    post_agent, attach): paths are sourced with SAC_* env vars; empty
    paths emit no source block.
  * sbatch directive rendering from SlurmSpec (partition, time, mem,
    gres, signal, extra_directives, logs_dir).
  * State file lifecycle (start writes jobid, stop clears).
  * squeue / scancel / sbatch invocation shape.

Hardener strings are the regression surface — if a test here fails,
decide whether the semantic change is intentional before updating the
assertion.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.config import (
    AgentConfig,
    ClaudeSpec,
    SlurmHeartbeatSpec,
    SlurmHooks,
    SlurmSpec,
)
from scitex_agent_container.runtimes import slurm as slurm_mod
from scitex_agent_container.runtimes.slurm import (
    HEARTBEAT_LOOP_MARKER,
    HEARTBEAT_START_MARKER,
    REQUIRED_EXIT_TRAP_MARKER,
    REQUIRED_HOLD_DEFAULT,
    REQUIRED_SHEBANG,
    REQUIRED_STRICT_MODE,
    REQUIRED_USR1_TRAP_MARKER,
    REQUIRED_XTRACE,
    SlurmRuntime,
    render_attach_command,
    render_sbatch_script,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(
    *,
    name: str = "head-spartan",
    workdir: str = "~/.scitex/agent-container/workspaces/head-spartan",
    slurm: SlurmSpec | None = None,
    claude: ClaudeSpec | None = None,
) -> AgentConfig:
    return AgentConfig(
        name=name,
        runtime="slurm",
        model="opus[1m]",
        workdir=workdir,
        claude=claude or ClaudeSpec(flags=["--dangerously-skip-permissions"]),
        slurm=slurm or SlurmSpec(partition="sapphire", time_limit="7-00:00:00"),
    )


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Isolate slurm state dir so tests don't collide with each other or
    with a real fleet on the developer's machine."""
    state_dir = tmp_path / "slurm-state"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_SLURM_STATE_DIR", str(state_dir))
    yield state_dir


# ---------------------------------------------------------------------------
# Hardener regression
# ---------------------------------------------------------------------------


class TestHardeners:
    def test_shebang_present(self):
        assert render_sbatch_script(_cfg()).startswith(REQUIRED_SHEBANG)

    def test_strict_mode_present(self):
        assert REQUIRED_STRICT_MODE in render_sbatch_script(_cfg())

    def test_xtrace_present(self):
        assert REQUIRED_XTRACE in render_sbatch_script(_cfg())

    def test_log_redirect_present(self):
        script = render_sbatch_script(_cfg())
        # ~ is expanded at render time because bash doesn't expand ~ inside
        # double-quoted strings (observed live on spartan job 24061388).
        from pathlib import Path

        expanded = Path("~/slurm_logs").expanduser()
        assert f'exec > "{expanded}/${{SLURM_JOB_ID:-nojob}}.out" 2>&1' in script

    def test_logs_dir_tilde_expanded(self):
        """Regression for spartan: literal ~ in double-quoted paths caused
        the wrapper to write to /home/user/~/slurm_logs/ and cd into a
        literal ~ directory, failing the job in 2 seconds (todo#425-b)."""
        from pathlib import Path

        script = render_sbatch_script(_cfg())
        assert "~/slurm_logs" not in script
        assert str(Path("~/slurm_logs").expanduser()) in script

    def test_workdir_tilde_expanded_and_mkdir(self):
        """Regression: workdir must be expanded + mkdir'd before cd."""
        from pathlib import Path

        script = render_sbatch_script(
            _cfg(workdir="~/.scitex/agent-container/workspaces/head-spartan")
        )
        expanded = Path(
            "~/.scitex/agent-container/workspaces/head-spartan"
        ).expanduser()
        assert f'mkdir -p "{expanded}"' in script
        assert f'cd "{expanded}"' in script

    def test_exit_trap_present(self):
        """Drop-through from the hold must be surfaced, not silently reaped."""
        script = render_sbatch_script(_cfg())
        assert REQUIRED_EXIT_TRAP_MARKER in script
        assert "trap 'rc=$?" in script
        assert "EXIT" in script

    def test_usr1_trap_present_with_auto_resubmit(self):
        """Walltime auto-resubmit must set the USR1 trap (llama-on-slurm pattern)."""
        script = render_sbatch_script(_cfg())
        assert REQUIRED_USR1_TRAP_MARKER in script
        assert f"trap {REQUIRED_USR1_TRAP_MARKER} USR1" in script
        assert 'sbatch "$0"' in script

    def test_usr1_trap_still_present_when_auto_resubmit_disabled(self):
        """Even with auto_resubmit disabled, the trap is installed — it just
        logs and skips the sbatch call so the walltime_signal hook still fires."""
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(auto_resubmit=False)))
        assert f"trap {REQUIRED_USR1_TRAP_MARKER} USR1" in script
        assert "auto_resubmit disabled" in script
        assert 'sbatch "$0"' not in script

    def test_persistent_hold_default(self):
        script = render_sbatch_script(_cfg())
        assert REQUIRED_HOLD_DEFAULT in script

    def test_persistent_hold_overrideable(self):
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(hold="sleep 3600")))
        assert "sleep 3600" in script

    def test_signal_directive_from_slurm_spec(self):
        script = render_sbatch_script(_cfg())
        assert "#SBATCH --signal=B:USR1@3600" in script

    def test_custom_signal_directive(self):
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(signal="B:USR2@1800")))
        assert "#SBATCH --signal=B:USR2@1800" in script


# ---------------------------------------------------------------------------
# SBATCH directives
# ---------------------------------------------------------------------------


class TestDirectives:
    def test_partition_and_time(self):
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(partition="sapphire", time_limit="3-00:00:00"))
        )
        assert "#SBATCH --partition=sapphire" in script
        assert "#SBATCH --time=3-00:00:00" in script

    def test_partition_omitted_when_empty(self):
        """Empty partition produces no --partition directive — SLURM uses cluster default."""
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(partition="")))
        assert "--partition" not in script

    def test_cpus_mem_nodes_ntasks(self):
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(cpus_per_task=4, mem="16G", nodes=2, ntasks=2))
        )
        assert "#SBATCH --cpus-per-task=4" in script
        assert "#SBATCH --mem=16G" in script
        assert "#SBATCH --nodes=2" in script
        assert "#SBATCH --ntasks=2" in script

    def test_gres_directive(self):
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(gres="gpu:1")))
        assert "#SBATCH --gres=gpu:1" in script

    def test_job_name_defaults_to_agent_name(self):
        script = render_sbatch_script(_cfg(name="my-agent"))
        assert "#SBATCH --job-name=my-agent" in script

    def test_job_name_override(self):
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(job_name="explicit-name")))
        assert "#SBATCH --job-name=explicit-name" in script

    def test_output_error_redirect_uses_logs_dir(self):
        script = render_sbatch_script(_cfg(slurm=SlurmSpec(logs_dir="/tmp/mylogs")))
        assert "#SBATCH --output=/tmp/mylogs/%x_%j.out" in script
        assert "#SBATCH --error=/tmp/mylogs/%x_%j.err" in script

    def test_extra_directives_appended(self):
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    extra_directives=[
                        "#SBATCH --qos=high",
                        "#SBATCH --exclusive",
                    ]
                )
            )
        )
        assert "#SBATCH --qos=high" in script
        assert "#SBATCH --exclusive" in script


# ---------------------------------------------------------------------------
# Hook contract
# ---------------------------------------------------------------------------


class TestHooks:
    def test_no_hooks_emits_no_source_blocks(self):
        script = render_sbatch_script(_cfg())
        # Only the walltime-signal handler's fallback comment should appear.
        assert "# Hook: pre_submit" not in script
        assert "# Hook: pre_agent" not in script
        assert "# Hook: walltime_signal" not in script
        assert "# Hook: post_agent" not in script

    def test_pre_agent_hook_sourced_with_env(self):
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(hooks=SlurmHooks(pre_agent="/path/to/pre-agent.sh")))
        )
        assert "# Hook: pre_agent" in script
        assert 'SAC_AGENT_ID="head-spartan"' in script
        assert 'SAC_PHASE="pre_agent"' in script
        assert 'source "/path/to/pre-agent.sh"' in script
        # File-existence guard so missing scripts don't kill the job.
        assert 'if [[ -f "/path/to/pre-agent.sh" ]]' in script

    def test_walltime_signal_hook_inside_trap(self):
        """walltime_signal must fire BEFORE the sbatch resubmit — otherwise
        the hook has no chance to post a heads-up before the new job runs."""
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(hooks=SlurmHooks(walltime_signal="/path/walltime.sh")))
        )
        # The handler function body must contain the hook source BEFORE sbatch.
        handler_start = script.index(f"{REQUIRED_USR1_TRAP_MARKER}()")
        resubmit_idx = script.index('sbatch "$0"', handler_start)
        hook_idx = script.index("walltime.sh", handler_start)
        assert hook_idx < resubmit_idx

    def test_post_agent_hook_sourced(self):
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(hooks=SlurmHooks(post_agent="/path/post.sh")))
        )
        assert "# Hook: post_agent" in script
        assert 'SAC_PHASE="post_agent"' in script
        assert 'source "/path/post.sh"' in script

    def test_hook_env_includes_all_sac_vars(self):
        script = render_sbatch_script(
            _cfg(slurm=SlurmSpec(hooks=SlurmHooks(pre_agent="/p.sh")))
        )
        # All five SAC_* vars must appear in the hook env block.
        for var in (
            "SAC_AGENT_ID",
            "SAC_WORKDIR",
            "SAC_LOG_FILE",
            "SAC_JOB_ID",
            "SAC_PHASE",
        ):
            assert var in script


# ---------------------------------------------------------------------------
# Heartbeat daemon (compute-node push loop)
# ---------------------------------------------------------------------------


class TestHeartbeat:
    """Regression for the head-spartan stale-heartbeat bug (lead msg#15654).

    Root cause: host-level heartbeat pushers (systemd/launchd) run on the
    login node and cannot see tmux sessions on the compute node the sbatch
    job landed on. Fix: spawn a compute-node-local push loop from the
    sbatch wrapper itself. This surface asserts the loop is emitted, is
    backgrounded, is interval-correct, and is cleaned up on wrapper exit.
    """

    def test_no_heartbeat_block_by_default(self):
        """Default (empty command) emits no heartbeat block — opt-in only.

        Non-HPC slurm users shouldn't pay for this; the hub registration
        path for them is whatever mechanism they've already set up.
        """
        script = render_sbatch_script(_cfg())
        assert HEARTBEAT_LOOP_MARKER not in script
        assert HEARTBEAT_START_MARKER not in script

    def test_heartbeat_block_emitted_when_command_set(self):
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="python3 /path/to/agent_meta.py --push",
                        interval_s=30,
                    )
                )
            )
        )
        assert HEARTBEAT_LOOP_MARKER in script
        assert HEARTBEAT_START_MARKER in script
        assert "python3 /path/to/agent_meta.py --push" in script

    def test_heartbeat_respects_custom_interval(self):
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="true",
                        interval_s=15,
                    )
                )
            )
        )
        # Interval appears in both the function body and the echo marker.
        assert "sleep 15" in script
        assert "interval=15s" in script

    def test_heartbeat_runs_in_background(self):
        """The loop must be backgrounded — otherwise it blocks claude-code
        from ever reaching the persistent hold and SLURM instantly
        reaps the job. Assert the ``&`` backgrounding marker."""
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="true",
                        interval_s=30,
                    )
                )
            )
        )
        # Either the setsid branch or the fallback branch backgrounds.
        assert ">> " in script
        assert "SAC_HEARTBEAT_PID=$!" in script

    def test_heartbeat_cleanup_on_exit_trap(self):
        """The EXIT trap must kill SAC_HEARTBEAT_PID so the loop never
        outlives the wrapper. Otherwise walltime-auto-resubmit doubles
        up pushers every resubmission cycle."""
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="true",
                    )
                )
            )
        )
        # The enhanced EXIT trap must reference SAC_HEARTBEAT_PID.
        assert 'kill "${SAC_HEARTBEAT_PID:-0}"' in script

    def test_heartbeat_log_file_default_under_logs_dir(self):
        """Default log file lives next to the job log under logs_dir so
        operators can diagnose push failures without extra plumbing."""
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(command="true"),
                )
            )
        )
        from pathlib import Path

        expanded = Path("~/slurm_logs").expanduser()
        assert f"{expanded}/${{SLURM_JOB_ID:-nojob}}.heartbeat.log" in script

    def test_heartbeat_log_file_override_is_expanded(self):
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="true",
                        log_file="~/my-hb.log",
                    )
                )
            )
        )
        from pathlib import Path

        expanded = Path("~/my-hb.log").expanduser()
        # ~ must be expanded — bash does not expand ~ inside double-quoted
        # strings (same class of bug as todo#425-b for logs_dir).
        assert "~/my-hb.log" not in script
        assert str(expanded) in script

    def test_heartbeat_emitted_after_tmux_session(self):
        """The loop must be spawned *after* tmux new-session so the local
        agent is visible to the push command on its first tick. Otherwise
        the first push reports alive=false and the hub flaps."""
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(command="true"),
                )
            )
        )
        tmux_idx = script.index("tmux new-session")
        hb_idx = script.index(HEARTBEAT_START_MARKER)
        assert tmux_idx < hb_idx

    def test_heartbeat_emitted_after_pre_agent_hook(self):
        """pre_agent hook exports fleet identity env vars
        (SCITEX_OROCHI_AGENT / SCITEX_OROCHI_TOKEN / HOSTNAME) that the
        push command needs in its environment. Must be sourced *before*
        the heartbeat loop spawns, else heartbeats push with the wrong
        (or empty) identity and the hub rejects them."""
        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    hooks=SlurmHooks(pre_agent="/path/pre.sh"),
                    heartbeat=SlurmHeartbeatSpec(command="true"),
                )
            )
        )
        pre_agent_idx = script.index("# Hook: pre_agent")
        hb_idx = script.index(HEARTBEAT_START_MARKER)
        assert pre_agent_idx < hb_idx

    def test_heartbeat_rendered_bash_syntax_is_valid(self, tmp_path):
        """Regression: the rendered script must pass ``bash -n``. Bash
        syntax errors in the heartbeat block would silently kill the job
        before claude-code ever spawns."""
        import subprocess

        script = render_sbatch_script(
            _cfg(
                slurm=SlurmSpec(
                    heartbeat=SlurmHeartbeatSpec(
                        command="python3 /tmp/fake_pusher.py --push",
                        interval_s=30,
                    ),
                )
            )
        )
        p = tmp_path / "wrapper.sh"
        p.write_text(script)
        proc = subprocess.run(
            ["bash", "-n", str(p)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Attach command
# ---------------------------------------------------------------------------


class TestAttach:
    def test_attach_command_uses_srun_pty_tmux(self, isolated_state):
        (isolated_state).mkdir(parents=True, exist_ok=True)
        (isolated_state / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "12345"})
        )
        cmd = render_attach_command(_cfg())
        assert "srun --jobid=12345" in cmd
        assert "--pty" in cmd
        assert "tmux -L default attach -t cld-head-spartan" in cmd

    def test_attach_hook_prepended(self, isolated_state):
        (isolated_state).mkdir(parents=True, exist_ok=True)
        (isolated_state / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "99999"})
        )
        cmd = render_attach_command(
            _cfg(slurm=SlurmSpec(hooks=SlurmHooks(attach="/path/att.sh")))
        )
        assert 'SAC_AGENT_ID="head-spartan"' in cmd
        assert 'SAC_JOB_ID="99999"' in cmd
        assert 'bash "/path/att.sh"' in cmd


# ---------------------------------------------------------------------------
# Runtime lifecycle (mocked sbatch/squeue/scancel)
# ---------------------------------------------------------------------------


def _mock_run(stdout: str = "", returncode: int = 0, stderr: str = ""):
    class _Proc:
        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return _Proc()


class TestRuntime:
    def test_start_submits_and_records_jobid(self, isolated_state, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "sbatch":
                return _mock_run(stdout="Submitted batch job 42\n")
            if cmd[0] == "squeue":
                return _mock_run(stdout="")  # not running yet
            return _mock_run()

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        rt = SlurmRuntime()
        cfg = _cfg()
        assert rt.start(cfg) is True
        assert calls[-1][0] == "sbatch"
        state = json.loads((isolated_state / "head-spartan.json").read_text())
        assert state["job_id"] == "42"

    def test_start_raises_on_sbatch_failure(self, isolated_state, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "sbatch":
                return _mock_run(stdout="", stderr="bad partition", returncode=1)
            return _mock_run(stdout="")

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        rt = SlurmRuntime()
        with pytest.raises(RuntimeError, match="sbatch failed"):
            rt.start(_cfg())

    def test_start_raises_when_jobid_unparseable(self, isolated_state, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "sbatch":
                return _mock_run(stdout="weird output")
            return _mock_run(stdout="")

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        rt = SlurmRuntime()
        with pytest.raises(RuntimeError, match="no jobid parsed"):
            rt.start(_cfg())

    def test_is_running_true_when_squeue_returns_state(
        self, isolated_state, monkeypatch
    ):
        (isolated_state).mkdir(parents=True, exist_ok=True)
        (isolated_state / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "42"})
        )

        def fake_run(cmd, **kwargs):
            if cmd[0] == "squeue":
                return _mock_run(stdout="RUNNING\n")
            return _mock_run()

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        assert SlurmRuntime().is_running(_cfg()) is True

    def test_is_running_false_when_squeue_empty(self, isolated_state, monkeypatch):
        (isolated_state).mkdir(parents=True, exist_ok=True)
        (isolated_state / "head-spartan.json").write_text(
            json.dumps({"name": "head-spartan", "job_id": "42"})
        )

        def fake_run(cmd, **kwargs):
            return _mock_run(stdout="")

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        assert SlurmRuntime().is_running(_cfg()) is False

    def test_is_running_false_when_no_state(self, isolated_state):
        assert SlurmRuntime().is_running(_cfg()) is False

    def test_stop_scancels_and_clears_state(self, isolated_state, monkeypatch):
        (isolated_state).mkdir(parents=True, exist_ok=True)
        state_file = isolated_state / "head-spartan.json"
        state_file.write_text(json.dumps({"name": "head-spartan", "job_id": "42"}))

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _mock_run()

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        rt = SlurmRuntime()
        assert rt.stop(_cfg()) is True
        assert ["scancel", "42"] in calls
        assert not state_file.exists()

    def test_stop_noop_when_no_state(self, isolated_state):
        """Idempotent stop — safe to call even if nothing was started."""
        assert SlurmRuntime().stop(_cfg()) is True

    def test_start_writes_sbatch_script_file(
        self, isolated_state, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_run(cmd, **kwargs):
            if cmd[0] == "sbatch":
                return _mock_run(stdout="Submitted batch job 77\n")
            return _mock_run(stdout="")

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        SlurmRuntime().start(_cfg())
        script_path = (
            tmp_path
            / ".scitex"
            / "agent-container"
            / "slurm-scripts"
            / "head-spartan.sbatch"
        )
        assert script_path.exists()
        assert REQUIRED_SHEBANG in script_path.read_text()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRuntimeRegistration:
    def test_lifecycle_dispatches_to_slurm_runtime(self):
        from scitex_agent_container.lifecycle import _get_runtime

        rt = _get_runtime(AgentConfig(name="x", runtime="slurm"))
        assert isinstance(rt, SlurmRuntime)

    def test_unknown_runtime_still_raises(self):
        from scitex_agent_container.lifecycle import _get_runtime

        with pytest.raises(ValueError):
            _get_runtime(AgentConfig(name="x", runtime="bogus"))


# ---------------------------------------------------------------------------
# Phase 3: dual-write to scitex-hpc Reservation lease
# ---------------------------------------------------------------------------


class TestHpcReservationDualWrite:
    """Verify SlurmRuntime.start/stop dual-write to scitex-hpc lease state.

    Best-effort semantics: if scitex-hpc is missing, sac still works
    (the import is wrapped in try/except inside slurm.py).
    """

    def test_register_called_after_successful_submit(
        self, isolated_state, monkeypatch, tmp_path
    ):
        """After sbatch parses jobid, _maybe_register_hpc_reservation fires."""
        called: list[tuple] = []

        def fake_register(cfg, job_id):
            called.append((cfg.name, job_id))

        monkeypatch.setattr(
            slurm_mod, "_maybe_register_hpc_reservation", fake_register
        )

        def fake_run(cmd, **kwargs):
            if cmd[0] == "sbatch":
                return _mock_run(stdout="Submitted batch job 4242\n")
            if cmd[0] == "squeue":
                return _mock_run(stdout="")  # not running yet (force=False path)
            return _mock_run()

        monkeypatch.setattr(slurm_mod.subprocess, "run", fake_run)
        cfg = _cfg(name="head-spartan-cpu")
        rt = SlurmRuntime()
        assert rt.start(cfg) is True

        assert called == [("head-spartan-cpu", "4242")]

    def test_clear_called_after_stop(
        self, isolated_state, monkeypatch, tmp_path
    ):
        """After scancel, _maybe_clear_hpc_reservation fires with the agent name."""
        # Pre-seed sac state so stop() has something to scancel
        (isolated_state).mkdir(parents=True, exist_ok=True)
        (isolated_state / "head-spartan-cpu.json").write_text(
            json.dumps({"name": "head-spartan-cpu", "job_id": "4242"})
        )

        cleared: list[str] = []

        def fake_clear(name):
            cleared.append(name)

        monkeypatch.setattr(
            slurm_mod, "_maybe_clear_hpc_reservation", fake_clear
        )
        monkeypatch.setattr(
            slurm_mod.subprocess, "run", lambda *a, **kw: _mock_run()
        )

        cfg = _cfg(name="head-spartan-cpu")
        SlurmRuntime().stop(cfg)
        assert cleared == ["head-spartan-cpu"]

    def test_register_swallows_import_error_when_hpc_missing(
        self, isolated_state, monkeypatch
    ):
        """sac must keep working if scitex-hpc isn't installed."""
        # Force the import inside the helper to fail
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "scitex_hpc":
                raise ImportError("simulated: scitex-hpc not installed")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Must not raise
        slurm_mod._maybe_register_hpc_reservation(_cfg(name="x"), "1")
        slurm_mod._maybe_clear_hpc_reservation("x")

    def test_register_swallows_unexpected_exception(self, monkeypatch):
        """A buggy scitex-hpc must not break sac's start path."""
        class FakeReservation:
            @staticmethod
            def from_jobid(**kwargs):
                raise RuntimeError("bug in scitex-hpc")

        fake_module = type("M", (), {"Reservation": FakeReservation})()
        monkeypatch.setitem(__import__("sys").modules, "scitex_hpc", fake_module)
        # Must not raise; warning is logged
        slurm_mod._maybe_register_hpc_reservation(_cfg(name="x"), "1")
