"""Tests for the CLI commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli import main
from scitex_agent_container.cli_pkg._helpers._agent_list_probe import (
    LocalProbe as _LocalProbe,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

VALID_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "/home/agent/work",
        "apptainer": {"image": "/x.sif", "binds": []},
        "claude": {"model": "sonnet"},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}


def _write_config(data: dict, name: str = "cli-test") -> str:
    """Write config under <tmp>/<name>/<name>.yaml (dir-as-SSoT)."""
    import copy

    from tests.scitex_agent_container._helpers.explicit_spec import (
        deep_merge,
        explicit_spec_defaults,
    )

    data = copy.deepcopy(data)
    # Red-start ruling 2026-07-21: every spec field explicit (fixture wins).
    if isinstance(data.get("spec"), dict):
        data["spec"] = deep_merge(
            explicit_spec_defaults(data.get("kind", "Agent")), data["spec"]
        )
    metadata = data.get("metadata") or {}
    metadata.pop("name", None)
    if metadata:
        data["metadata"] = metadata
    elif "metadata" in data:
        del data["metadata"]
    tmp_dir = Path(tempfile.mkdtemp()) / name
    tmp_dir.mkdir(parents=True)
    path = tmp_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


class TestCLI:
    def test_main_help_flag_prints_program_banner(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help"])
        # Assert
        assert result.exit_code == 0 and "SciTeX Agent Container" in result.output

    def test_agents_check_with_valid_yaml_passes_validation(self):
        # Arrange
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        # check now does YAML validation first; runtime probes may fail in test
        # env (no docker), so we only assert validation passed by checking output.
        try:
            assert "validation failed" not in result.output.lower()
        finally:
            Path(path).unlink()

    def test_agents_check_with_wrong_apiversion_exits_nonzero(self):
        # Arrange
        data = {**VALID_CONFIG, "apiVersion": "wrong"}
        path = _write_config(data)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        try:
            assert result.exit_code != 0
        finally:
            Path(path).unlink()

    def test_agents_list_json_with_no_agents_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_stop_unknown_agent_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["stop", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_agents_health_unknown_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_agents_tail_unknown_name_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "tail", "nonexistent-agent"])
        # Assert
        assert result.exit_code != 0

    def test_db_clean_sweep_exits_zero(self, pg_schema: str):
        # Arrange
        # F-CS11 phase 5: `registry clean` was renamed to `db clean`.
        # The new path is the SQLite GC sweep — runs against state.db,
        # exits 0 with zero-or-more swept entries.
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["db", "clean"])
        # Assert
        assert result.exit_code == 0

    def test_main_version_flag_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--version"])
        # Assert
        assert result.exit_code == 0

    def test_main_version_flag_prints_version_label(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--version"])
        # Assert
        assert "version" in result.output

    def test_agents_list_json_empty_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_agents_list_json_empty_returns_agents_payload(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--json"])
        # Assert
        # ``result.output`` folds stderr in (click 8.4 dropped mix_stderr).
        # This command walks the AMBIENT fleet, so a spec whose account has
        # no saved snapshot logs a WARN during config load -- ahead of the
        # payload -- and json.loads dies at char 0. Intermittently, because
        # it depends on what is on disk. Parse the payload stream only.
        data = json.loads(result.stdout)
        assert isinstance(data, dict) and "agents" in data

    def test_removed_ps_command_exits_nonzero(self):
        """ps command was removed; list covers the same functionality."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["ps"])
        # Assert
        # ps no longer exists — should fail with usage error
        assert result.exit_code != 0

    def test_agents_list_unknown_name_json_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "nonexistent", "--json"])
        # Assert
        assert result.exit_code != 0

    def test_agents_list_unknown_name_json_emits_error_key(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "nonexistent", "--json"])
        # Assert
        data = json.loads(result.stdout)
        assert "error" in data

    def test_agents_health_unknown_name_json_exits_nonzero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent", "--json"])
        # Assert
        assert result.exit_code != 0

    def test_agents_health_unknown_name_json_emits_error_key(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "health", "nonexistent", "--json"])
        # Assert
        data = json.loads(result.stdout)
        assert "error" in data

    def test_list_python_apis_command_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis"])
        # Assert
        assert result.exit_code == 0

    def test_list_python_apis_command_prints_api_tree(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis"])
        # Assert
        assert "API tree" in result.output

    def test_list_python_apis_json_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis", "--json"])
        # Assert
        assert result.exit_code == 0

    def test_list_python_apis_json_returns_nonempty_list(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["list-python-apis", "--json"])
        # Assert
        data = json.loads(result.stdout)
        assert isinstance(data, list) and len(data) > 0

    def test_help_recursive_command_exits_zero(self):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help-recursive"])
        # Assert
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "expected_substring",
        ["Complete Command Reference", "start", "stop", "list-python-apis"],
    )
    def test_help_recursive_output_contains_substring(self, expected_substring):
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["--help-recursive"])
        # Assert
        assert expected_substring in result.output

    def test_agents_list_capability_filter_flag_accepted(self):
        """The --capability flag should be accepted even with no agents."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--capability", "gpu"])
        # Assert
        assert result.exit_code == 0

    def test_agents_list_machine_filter_flag_accepted(self):
        """The --machine flag should be accepted even with no agents."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "list", "--machine", "spartan"])
        # Assert
        assert result.exit_code == 0

    def _find_setup_gpu_agent(self, tmpdir):
        """Shared setup for agents-find tests below."""
        from tests.scitex_agent_container._helpers.explicit_spec import (
            explicit_spec,
        )

        # Red-start ruling 2026-07-21: every spec field explicit.
        config_with_caps = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {
                "labels": {
                    "role": "head",
                    "machine": "spartan",
                    "capabilities": "gpu,slurm,ml-training",
                },
            },
            "spec": explicit_spec(
                {
                    "runtime": "apptainer",
                    "host": "${HOSTNAME}",
                    "workdir": "/home/agent/work",
                    "apptainer": {"image": "/x.sif", "binds": []},
                    "claude": {"model": "sonnet"},
                    "health": {"enabled": True, "interval": 60},
                    "restart": {"policy": "on-failure", "max_retries": 3},
                }
            ),
        }
        agent_dir = Path(tmpdir) / "test-gpu-agent"
        agent_dir.mkdir()
        path = agent_dir / "spec.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(config_with_caps, f)
        runner = CliRunner()
        return runner.invoke(main, ["agents", "find", "gpu", "--dir", tmpdir, "--json"])

    def test_agents_find_in_directory_exits_zero(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            assert result.exit_code == 0

    def test_agents_find_in_directory_returns_one_match(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.stdout)
            assert len(data) == 1

    def test_agents_find_in_directory_match_has_expected_name(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.stdout)
            assert data[0]["name"] == "test-gpu-agent"

    def test_agents_find_in_directory_match_has_gpu_capability(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_gpu_agent(tmpdir)
            # Assert
            data = json.loads(result.stdout)
            assert "gpu" in data[0]["capabilities"]

    def test_agents_check_local_agent_runs_preflight(self):
        """check command should run preflight checks on a local agent."""
        # Arrange
        path = _write_config(VALID_CONFIG)
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["agents", "check", path])
        # Assert
        # Should succeed on a local machine that has python and screen
        # Even if screen is missing, the command itself should not crash
        try:
            assert "Checking" in result.output
        finally:
            Path(path).unlink()

    def test_start_help_exits_zero(self):
        """start --help should exit cleanly."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["start", "--help"])
        # Assert
        assert result.exit_code == 0

    def test_start_help_shows_no_preflight_option(self):
        """start --help should show the --no-preflight option."""
        # Arrange
        runner = CliRunner()
        # Act
        result = runner.invoke(main, ["start", "--help"])
        # Assert
        assert "--no-preflight" in result.output

    def _find_setup_no_match(self, tmpdir):
        """Shared setup for agents-find no-match test."""
        config_no_match = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {
                "name": "basic-agent",
                "labels": {"role": "head"},
            },
            "spec": {"runtime": "apptainer"},
        }
        path = Path(tmpdir) / "basic.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(config_no_match, f)
        runner = CliRunner()
        return runner.invoke(main, ["agents", "find", "gpu", "--dir", tmpdir])

    def test_agents_find_no_match_exits_zero(self):
        """find command should exit 0 when no agents match."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_no_match(tmpdir)
            # Assert
            assert result.exit_code == 0

    def test_agents_find_no_match_reports_no_agents_found(self):
        """find command should print a no-match message when nothing matches."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            # Act
            result = self._find_setup_no_match(tmpdir)
            # Assert
            assert "No agents found" in result.output


# ----------------------------------------------------------------------------
# Regression tests for todo#254 — list --json must not block when SSH
# fan-out hits a timeout. Per-probe timeout + parallel fan-out keeps the
# whole list command bounded instead of 5s-timeout-blocking per-agent.
# ----------------------------------------------------------------------------


class _HangingRuntime:
    """Fake runtime that simulates a hung probe (todo#254 regression).

    ``release`` is set by the caller once the measurement window closes. The
    probe still blocks far past the 1s budget while it is being timed -- that
    is the point -- but a worker thread cannot outlive the test that started
    it. ``pool.shutdown(wait=False)`` deliberately does not join these
    threads, so an unconditional ``sleep(10)`` leaves them running inside the
    xdist worker long after the test returned.
    """

    def __init__(self, release=None):
        self._release = release

    def is_running(self, cfg):
        import time

        if self._release is not None:
            self._release.wait(10)  # longer than the probe timeout
        else:
            time.sleep(10)
        return True


class _FakeRegistryTwoAgents:
    def __init__(self, remote_cfg_path, local_cfg_path):
        self._remote_cfg_path = remote_cfg_path
        self._local_cfg_path = local_cfg_path

    def list_all(self):
        return [
            {
                "name": "test-remote",
                "screen": "test-remote",
                "config": str(self._remote_cfg_path),
                "started_at": "?",
            },
            {
                "name": "test-local",
                "screen": "test-local",
                "config": str(self._local_cfg_path),
                "started_at": "?",
            },
        ]


class _FakeRegistryOneAgent:
    def __init__(self, cfg_path):
        self._cfg_path = cfg_path

    def list_all(self):
        return [
            {
                "name": "test-fast",
                "screen": "test-fast",
                "config": str(self._cfg_path),
                "started_at": "?",
            }
        ]


class TestListJsonTimeoutBudget:
    """todo#254 regression suite.

    Pre-fix: a hung SSH probe for one remote agent would block the entire
    `scitex-agent-container list --json` command past its 5s smoke-test
    budget, because ClaudeCodeRuntime().is_running(cfg) was called serially
    per agent in get_agent_list_data.

    Post-fix: each remote probe has a per-probe timeout (default 2s) and
    is run in a ThreadPoolExecutor. Probes that time out produce
    status="unknown" + liveness_unknown=True in the output row.
    """

    # ``host`` is REQUIRED since the explicit-placement directive
    # (2026-06-23). Without it ``load_config`` raises, ``_agent_list`` swallows
    # that into ``cfg = None``, the agent never enters ``probe_targets``, and
    # the fake probe below IS NEVER CALLED — while the row still reads
    # status="unknown" for that unrelated reason. All three hanging-probe
    # tests then passed while guarding nothing (measured 2026-08-07: 0 probe
    # invocations). ``_run_fast_probe`` already carried ``host``; this fixture
    # was missed. The ``calls`` counter below is the positive control that
    # makes a repeat of that silent voiding FAIL instead of going green.
    _SPEC = """apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: ${HOSTNAME}
"""

    @staticmethod
    def _run_probe(tmp_path, probe_factory, release=None):
        import time

        from scitex_agent_container.cli_pkg import _helpers

        # v3 dir-as-SSoT: name comes from parent dir.
        rd = tmp_path / "test-remote"
        rd.mkdir()
        remote_cfg_path = rd / "test-remote.yaml"
        remote_cfg_path.write_text(explicitize_yaml(TestListJsonTimeoutBudget._SPEC))
        ld = tmp_path / "test-local"
        ld.mkdir()
        local_cfg_path = ld / "test-local.yaml"
        local_cfg_path.write_text(explicitize_yaml(TestListJsonTimeoutBudget._SPEC))

        calls = []

        def _counting_probe(cfg):
            calls.append(cfg)
            return probe_factory(cfg)

        # PA-306: hand-rolled fake injection with explicit restore.
        saved_probe = getattr(_helpers, "probe_local_detail", None)
        _helpers.probe_local_detail = _counting_probe
        try:
            t0 = time.monotonic()
            rows = _helpers.get_agent_list_data(
                _FakeRegistryTwoAgents(remote_cfg_path, local_cfg_path),
                remote_probe_timeout_s=1.0,
            )
            elapsed = time.monotonic() - t0
        finally:
            # Measurement window is closed: let any probe thread the pool
            # abandoned return NOW rather than sleep out its full hang.
            if release is not None:
                release.set()
            if saved_probe is None:
                if hasattr(_helpers, "probe_local_detail"):
                    delattr(_helpers, "probe_local_detail")
            else:
                _helpers.probe_local_detail = saved_probe
        return elapsed, rows, len(calls)

    @classmethod
    def _run_hanging_probe(cls, tmp_path):
        import threading

        release = threading.Event()
        runtime = _HangingRuntime(release)
        return cls._run_probe(
            tmp_path,
            lambda cfg: _LocalProbe(
                running=runtime.is_running(cfg),
                runtime="HangingRuntime",
                error=None,
            ),
            release=release,
        )

    @classmethod
    def _run_instant_probe(cls, tmp_path):
        """Baseline arm: identical call, only the hang removed."""
        return cls._run_probe(
            tmp_path,
            lambda cfg: _LocalProbe(running=True, runtime="InstantRuntime", error=None),
        )

    def test_hanging_probe_actually_reaches_the_probe_path(self, pg_schema: str, tmp_path):
        """Positive control for the whole hanging-probe suite.

        The other tests are only meaningful if the fake probe RUNS. A spec
        that fails validation silently skips it and every assertion below
        still passes, so assert the invocation directly.
        """
        # Arrange
        probe = self._run_hanging_probe
        # Act
        _elapsed, _rows, calls = probe(tmp_path)
        # Assert
        assert calls > 0, (
            "the hanging probe was never invoked — get_agent_list_data "
            "skipped the probe path (likely load_config rejected the "
            "fixture spec), so the todo#254 timeout guards are VACUOUS"
        )

    def test_hanging_remote_probe_respects_timeout_budget(self, pg_schema: str, tmp_path):
        """A hanging remote probe must not exceed the per-probe timeout."""
        # Arrange: baseline arm measures the same call WITHOUT the hang, so
        # the assertion charges the budget only for the hang. Asserting raw
        # wall clock instead made this flaky — the non-probe work (config
        # load, auth states, row build) reached 8.4s on a loaded CI runner
        # and was charged to the 1s probe timeout, failing and passing on
        # the SAME commit (1ebfb71f, 2026-08-06).
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        hang_dir = tmp_path / "hang"
        hang_dir.mkdir()
        baseline, _rows, _base_calls = self._run_instant_probe(base_dir)
        # Act
        elapsed, _rows, _calls = self._run_hanging_probe(hang_dir)
        # Assert
        # (that both arms reached the probe path is asserted separately by
        # test_hanging_probe_actually_reaches_the_probe_path — the positive
        # control that makes this comparison meaningful.)
        overshoot = elapsed - baseline
        # The 10s hang must cost no more than its 1s budget (+ pool overhead).
        assert overshoot < 2.5, (
            f"a hung probe added {overshoot:.1f}s over the {baseline:.1f}s "
            f"baseline despite a 1s probe timeout — todo#254 regression "
            f"re-introduced"
        )

    def test_hanging_remote_probe_marks_status_unknown(self, pg_schema: str, tmp_path):
        """A timed-out remote probe must produce status='unknown'."""
        # Arrange
        probe = self._run_hanging_probe
        # Act
        _elapsed, rows, _calls = probe(tmp_path)
        # Assert
        remote_row = next(r for r in rows if r["name"] == "test-remote")
        assert remote_row["status"] == "unknown"

    def test_hanging_remote_probe_marks_liveness_unknown_true(self, pg_schema: str, tmp_path):
        """A timed-out remote probe must set liveness_unknown=True."""
        # Arrange
        probe = self._run_hanging_probe
        # Act
        _elapsed, rows, _calls = probe(tmp_path)
        # Assert
        remote_row = next(r for r in rows if r["name"] == "test-remote")
        assert remote_row.get("liveness_unknown") is True

    @staticmethod
    def _run_fast_probe(tmp_path):
        from scitex_agent_container.cli_pkg import _helpers

        d = tmp_path / "test-fast"
        d.mkdir()
        cfg_path = d / "test-fast.yaml"
        cfg_path.write_text(
            explicitize_yaml("""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: ${HOSTNAME}
  workdir: /home/agent/work
  apptainer:
    image: /x.sif
    binds: []
  claude:
    model: sonnet
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
""")
        )

        # PA-306: hand-rolled fake injection with explicit restore.
        saved_probe = getattr(_helpers, "probe_local_detail", None)
        _helpers.probe_local_detail = lambda cfg: _LocalProbe(
            running=True, runtime="TestRuntime", error=None
        )
        try:
            rows = _helpers.get_agent_list_data(
                _FakeRegistryOneAgent(cfg_path), remote_probe_timeout_s=5.0
            )
        finally:
            if saved_probe is None:
                if hasattr(_helpers, "probe_local_detail"):
                    delattr(_helpers, "probe_local_detail")
            else:
                _helpers.probe_local_detail = saved_probe
        return rows[0]

    def test_fast_remote_probe_reports_running_status(self, pg_schema: str, tmp_path):
        """A fast remote probe must report status='running'."""
        # Arrange
        probe = self._run_fast_probe
        # Act
        row = probe(tmp_path)
        # Assert
        assert row["status"] == "running"

    def test_fast_remote_probe_is_not_marked_liveness_unknown(self, pg_schema: str, tmp_path):
        """A fast remote probe must NOT be marked liveness_unknown."""
        # Arrange
        probe = self._run_fast_probe
        # Act
        row = probe(tmp_path)
        # Assert
        assert row.get("liveness_unknown") is not True
