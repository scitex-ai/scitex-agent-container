"""Tests for cli_pkg.lifecycle._dispatch._dispatch_remote_start (step 4).

See ``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``.
Covers the drift-check / rsync surface (step 3b) AND the remote
``sac agents start`` invocation + JSON parse + lead-side instances
row write (step 4).

No-mocks: real ``subprocess.run`` against PATH-prepended fake
``rsync`` and ``ssh`` binaries. Conforms to STX-TQ002 (AAA markers),
STX-TQ003 (descriptive names), STX-TQ007 (one assert per test).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._dispatch import (
    _dispatch_remote_start,
    lookup_remote_peer,
    try_dispatch,
    try_dispatch_remote,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import HostsSpec

# ---------------------------------------------------------------------------
# Shim helpers — dual-behavior rsync (dry-run vs real) plus a fake ssh.
# ---------------------------------------------------------------------------


def _install_rsync_shim(
    bin_dir: Path,
    *,
    dry_stdout: str = "",
    dry_stderr: str = "",
    dry_exit: int = 0,
    real_stdout: str = "",
    real_stderr: str = "",
    real_exit: int = 0,
) -> Path:
    """rsync shim that branches on ``-acvn`` (dry-run) vs ``-acv``."""
    log = bin_dir / "rsync.argv.jsonl"
    script = bin_dir / "rsync"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "is_dry = any(\n"
        "    a.startswith('-') and not a.startswith('--') and 'n' in a\n"
        "    for a in sys.argv[1:]\n"
        ")\n"
        f"d_out, d_err, d_rc = {json.dumps(dry_stdout)}, {json.dumps(dry_stderr)}, {int(dry_exit)}\n"
        f"r_out, r_err, r_rc = {json.dumps(real_stdout)}, {json.dumps(real_stderr)}, {int(real_exit)}\n"
        "out, err, rc = (d_out, d_err, d_rc) if is_dry else (r_out, r_err, r_rc)\n"
        "sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return script


def _install_ssh_shim(
    bin_dir: Path,
    *,
    stdout: str = "{}",
    stderr: str = "",
    exit: int = 0,
) -> Path:
    """ssh shim that records its argv (JSON list) and emits configured rc/stdout."""
    log = bin_dir / "ssh.argv.jsonl"
    script = bin_dir / "ssh"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        f"sys.stderr.write({json.dumps(stderr)})\n"
        f"sys.exit({int(exit)})\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return script


def _is_dry_run_argv(argv: list[str]) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and "n" in a for a in argv)


def _rsync_invocations(bin_dir: Path) -> list[list[str]]:
    log = bin_dir / "rsync.argv.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _ssh_invocations(bin_dir: Path) -> list[list[str]]:
    log = bin_dir / "ssh.argv.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _rsync_dry_count(bin_dir: Path) -> int:
    return sum(1 for argv in _rsync_invocations(bin_dir) if _is_dry_run_argv(argv))


def _rsync_real_count(bin_dir: Path) -> int:
    return sum(1 for argv in _rsync_invocations(bin_dir) if not _is_dry_run_argv(argv))


# ---------------------------------------------------------------------------
# Fixtures: HOME redirection, spec dir, PATH-prepended shim bin, peer config.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore):
    """Redirect HOME so Path.home() returns tmp_path."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def spec_dir(fake_home: Path) -> Path:
    """Populated spec dir at ``~/.scitex/agent-container/agents/alpha``."""
    d = fake_home / ".scitex" / "agent-container" / "agents" / "alpha"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: alpha\n")
    return d


@pytest.fixture
def shim_bin(tmp_path: Path, env_save_restore) -> Path:
    """Prepend a fresh bin dir to PATH for shim installation."""
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir()
    saved_path = os.environ.get("PATH", "")
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{saved_path}")
    return bin_dir


@pytest.fixture
def state_db(fake_home: Path) -> Path:
    """Redirect state.db to a tmp path under fake_home.

    DEFAULT_DB_PATH is module-level and reads the env var at import
    time, so we reload the module after setting the env var. Tests
    that mutate state.db rely on this to stay isolated; without the
    reload each test would write to the user's real state.db.

    Both env-var manipulation and module reload are managed locally
    (no env_save_restore) so the teardown order is unambiguous: we
    first reset the env, THEN reload, so DEFAULT_DB_PATH lands back
    on the real path the user expects after the fixture exits.
    """
    import importlib
    import os as _os

    db = fake_home / "state.db"
    saved = _os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield db
    finally:
        if saved is None:
            _os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved
        importlib.reload(_state_db_mod)


def _write_peer_config(
    home: Path,
    env_save_restore,
    peer: str = "peer-host",
    env_preamble: list[str] | None = None,
) -> Path:
    """Write ``config.yaml`` registering ``peer`` (optional env_preamble)."""
    cfg = home / "config.yaml"
    body = f"host:\n  fallback: hostname-short\npeers:\n  {peer}:\n    ssh: {peer}\n"
    if env_preamble:
        body += "    env_preamble:\n"
        for line in env_preamble:
            body += f"      - {line}\n"
    cfg.write_text(body)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


# ---------------------------------------------------------------------------
# Scenario builder.
# ---------------------------------------------------------------------------


@dataclass
class _Scenario:
    bin_dir: Path
    raised: BaseException | None = None
    returned: Any = None
    captured_stdout: str = ""
    captured_stderr: str = ""

    @property
    def message(self) -> str:
        return str(self.raised) if self.raised is not None else ""

    @property
    def dry_count(self) -> int:
        return _rsync_dry_count(self.bin_dir)

    @property
    def real_count(self) -> int:
        return _rsync_real_count(self.bin_dir)


def _act_dispatch(
    shim_bin: Path,
    capsys,
    *,
    rsync_kwargs: dict[str, Any] | None = None,
    ssh_kwargs: dict[str, Any] | None = None,
    name: str = "alpha",
    peer: str = "peer-host",
    dry_run: bool = False,
    force: bool = False,
) -> _Scenario:
    """Install shim(s) and invoke ``_dispatch_remote_start`` once."""
    _install_rsync_shim(shim_bin, **(rsync_kwargs or {}))
    if ssh_kwargs is not None:
        _install_ssh_shim(shim_bin, **ssh_kwargs)
    scen = _Scenario(bin_dir=shim_bin)
    try:
        scen.returned = _dispatch_remote_start(name, peer, dry_run=dry_run, force=force)
    except BaseException as exc:
        scen.raised = exc
    captured = capsys.readouterr()
    scen.captured_stdout = captured.out
    scen.captured_stderr = captured.err
    return scen


_FIRST_LAUNCH_OUTPUT = ">f+++++++++ spec.yaml\ncd+++++++++ overlays/\n"
_DRIFT_OUTPUT = ">f.st...... spec.yaml\n>f+++++++++ NEWFILE.txt\n"
_OK_JSON = '{"a2a_port": 47213, "started_at": "2026-05-16T00:00:00Z"}'

# Reusable step-4 shim kwargs (clean rsync + ok ssh).
_RK_OK = dict(dry_stdout=_FIRST_LAUNCH_OUTPUT, dry_exit=0, real_exit=0)
_SK_OK = dict(stdout=_OK_JSON, exit=0)


# ---------------------------------------------------------------------------
# Drift / rsync gate behavior (step 3b).
# ---------------------------------------------------------------------------


class TestDispatchDriftBlocksWithoutForce:
    def test_drift_without_force_raises_runtime_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_drift_message_mentions_spec_drift(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert "Spec drift" in scen.message

    def test_drift_does_not_invoke_real_rsync(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert scen.real_count == 0


class TestDispatchDryRunMode:
    def test_dry_run_mode_does_not_raise(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk, dry_run=True)
        # Assert
        assert scen.raised is None

    def test_dry_run_mode_returns_zero_exit(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk, dry_run=True)
        # Assert
        assert scen.returned == 0

    def test_dry_run_mode_prints_dispatch_marker(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk, dry_run=True)
        # Assert
        assert "[dispatch] dry-run" in scen.captured_stdout

    def test_dry_run_mode_skips_real_rsync(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk, dry_run=True)
        # Assert
        assert scen.real_count == 0


class TestDispatchRsyncFailures:
    def test_dry_run_failure_raises_runtime_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stderr="ssh: host unreachable\n", dry_exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dry_run_failure_message_identifies_phase(self, spec_dir, shim_bin, capsys):
        # Arrange
        rk = dict(dry_stderr="ssh: host unreachable\n", dry_exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert "rsync --dry-run failed" in scen.message

    def test_real_rsync_failure_message_mentions_rsync_failed(
        self, spec_dir, shim_bin, capsys
    ):
        # Arrange — clean dry-run, fail on real rsync.
        rk = dict(dry_exit=0, real_stderr="broken pipe\n", real_exit=12)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert "rsync failed" in scen.message

    def test_real_rsync_failure_excludes_dry_run_phase(
        self, spec_dir, shim_bin, capsys
    ):
        # Arrange — disambiguate from dry-run failure.
        rk = dict(dry_exit=0, real_stderr="broken pipe\n", real_exit=12)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=rk)
        # Assert
        assert "rsync --dry-run failed" not in scen.message


class TestDispatchMissingSpecDir:
    def test_missing_spec_dir_raises_file_not_found(self, fake_home, shim_bin, capsys):
        # Arrange — fake_home redirects HOME but NO spec dir is created.
        # Act
        scen = _act_dispatch(shim_bin, capsys, name="ghost")
        # Assert
        assert isinstance(scen.raised, FileNotFoundError)

    def test_missing_spec_dir_message_names_problem(self, fake_home, shim_bin, capsys):
        # Arrange
        # Act
        scen = _act_dispatch(shim_bin, capsys, name="ghost")
        # Assert
        assert "Spec dir for" in scen.message


# ---------------------------------------------------------------------------
# Step 4 — ssh handoff: success path writes lead-side row, returns 0,
# prints success line; ssh failure / non-JSON paths raise RuntimeError.
# ---------------------------------------------------------------------------


class TestDispatchSshSuccessPath:
    def test_dispatch_ssh_success_writes_instances_row(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert — query state.db via the project API so schema is init'd.
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert (rows[0]["host"], rows[0]["a2a_port"]) == ("peer-host", 47213)

    def test_dispatch_ssh_success_returns_zero(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert scen.returned == 0

    def test_dispatch_ssh_success_prints_started_message(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert "started on 'peer-host'" in scen.captured_stdout

    def test_dispatch_ssh_success_prints_assigned_port(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert "a2a_port=47213" in scen.captured_stdout

    def test_dispatch_ssh_success_marks_row_remote(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — a cross-host dispatch must record remote=1 so
        # resolve_peer_url / agent_status know to reach the agent on the
        # peer (sac-agent-spawn design, Rule B/F).
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["remote"] == 1

    def test_dispatch_ssh_success_records_bound_port_from_peer_json(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — the bound port captured back from the peer's --json
        # output is the concrete int the peer's allocator resolved (the
        # crux of the remote-port gap fix).
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["bound_port"] == 47213

    def test_dispatch_ssh_success_records_cli_spawned_by(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — a bare lead dispatch (no SAC_NAME) records the
        # lineage edge as "cli".
        env_save_restore.set("SAC_NAME", "")
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["spawned_by"] == "cli"

    def test_dispatch_ssh_success_propagates_a2a_port_none_when_spec_omits_it(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — when peer JSON has ``a2a_port: null`` (sidecar
        # disabled), lead MUST write NULL into the instances row
        # rather than substituting a default. Covers the cross-host
        # null-propagation seam.
        _write_peer_config(fake_home, env_save_restore)
        sk_null = dict(
            stdout='{"a2a_port": null, "started_at": "2026-05-17T00:00:00Z"}',
            exit=0,
        )
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=sk_null)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["a2a_port"] is None


class TestDispatchSshFailurePaths:
    def test_dispatch_ssh_failure_raises_runtime_error(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = dict(stdout="", stderr="connection refused\n", exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dispatch_ssh_failure_message_mentions_remote_failed(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = dict(stderr="connection refused\n", exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=sk)
        # Assert
        assert "Remote `sac agents start alpha` failed" in scen.message

    def test_dispatch_ssh_non_json_stdout_raises_runtime_error(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = dict(stdout="OK\n", exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dispatch_ssh_non_json_message_mentions_phase(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = dict(stdout="OK\n", exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=sk)
        # Assert
        assert "non-JSON stdout" in scen.message


# ---------------------------------------------------------------------------
# Step 4 — ssh argv assembly: --no-redispatch + env_preamble forwarding.
# ---------------------------------------------------------------------------


_LMOD_PREAMBLE = ["module load GCCcore/11.3.0", "module load Apptainer/1.3.3"]


class TestDispatchSshArgv:
    def test_dispatch_ssh_argv_includes_no_redispatch_flag(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer-side MUST NOT re-trigger the dispatch branch.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert "--no-redispatch" in " ".join(_ssh_invocations(shim_bin)[-1])

    def test_dispatch_ssh_argv_includes_json_flag(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer must emit machine-parseable output.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert "--json" in " ".join(_ssh_invocations(shim_bin)[-1])

    def test_dispatch_env_preamble_forwarded_via_build_ssh_argv(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer with env_preamble; build_ssh_argv wraps in
        # `bash -c '<preamble> && <cmd>'`.
        _write_peer_config(fake_home, env_save_restore, env_preamble=_LMOD_PREAMBLE)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert "module load Apptainer/1.3.3 && sac agents start" in " ".join(
            _ssh_invocations(shim_bin)[-1]
        )

    def test_dispatch_env_preamble_wrapper_uses_bash_lc(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — bash -c wrapper is the explicit env_preamble shape.
        _write_peer_config(fake_home, env_save_restore, env_preamble=_LMOD_PREAMBLE)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        assert any("bash -c" in tok for tok in _ssh_invocations(shim_bin)[-1])


# ---------------------------------------------------------------------------
# Bug 3 — TOFU policy: dispatch must add ``-o
# StrictHostKeyChecking=accept-new`` to BOTH the ssh handoff AND rsync's
# transport, so a first-touch peer (the most common dispatch failure
# mode on a freshly-configured cluster) does not silently rc-1 with no
# operator-actionable error.
# ---------------------------------------------------------------------------


class TestDispatchStrictHostKeyChecking:
    def test_dispatch_ssh_argv_includes_accept_new_strict_host_key(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert — the rendered ssh argv carries the TOFU policy.
        assert "StrictHostKeyChecking=accept-new" in " ".join(
            _ssh_invocations(shim_bin)[-1]
        )

    def test_dispatch_dry_rsync_argv_uses_accept_new_transport(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — clean first-launch dry-run, ok ssh.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert — rsync's -e transport carries the accept-new flag.
        dry = next(a for a in _rsync_invocations(shim_bin) if _is_dry_run_argv(a))
        assert any("StrictHostKeyChecking=accept-new" in tok for tok in dry)

    def test_dispatch_real_rsync_argv_uses_accept_new_transport(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, rsync_kwargs=_RK_OK, ssh_kwargs=_SK_OK)
        # Assert
        real = next(a for a in _rsync_invocations(shim_bin) if not _is_dry_run_argv(a))
        assert any("StrictHostKeyChecking=accept-new" in tok for tok in real)


# ---------------------------------------------------------------------------
# lookup_remote_peer + try_dispatch_remote: state.db-driven routing.
# ---------------------------------------------------------------------------


class TestLookupRemotePeer:
    def test_no_active_row_returns_none(self, fake_home, state_db, env_save_restore):
        # Arrange — fresh state.db with no instances row for "alpha".
        from scitex_agent_container._state.state_db import init_schema

        init_schema()
        # Act
        result = lookup_remote_peer("alpha")
        # Assert
        assert result is None

    def test_local_active_row_returns_none(self, fake_home, state_db, env_save_restore):
        # Arrange — write a row whose host matches the current_host (so
        # ``state_db._resolve_host`` will collapse to the same value).
        env_save_restore.set("SAC_HOST", "local-host-x")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="local-host-x")
        # Act
        result = lookup_remote_peer("alpha")
        # Assert
        assert result is None

    def test_remote_active_row_returns_peer_and_row(
        self, fake_home, state_db, env_save_restore
    ):
        # Arrange — row's host differs from this run's current_host.
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="peer-host", a2a_port=18888)
        # Act
        peer, row = lookup_remote_peer("alpha")  # type: ignore[misc]
        # Assert
        assert (peer, row["a2a_port"]) == ("peer-host", 18888)


class TestTryDispatchRemote:
    def _peers_with(self, *names):
        from scitex_agent_container._state.host_config import PeerSpec

        return {n: PeerSpec(name=n, ssh=n) for n in names}

    def test_no_active_row_returns_false(self, fake_home, state_db, env_save_restore):
        # Arrange — no row; caller proceeds local.
        from scitex_agent_container._state.state_db import init_schema

        init_schema()
        calls: list = []
        # Act
        dispatched = try_dispatch_remote(
            "ghost",
            "stop",
            self._peers_with("peer-host"),
            handler=lambda p, r, ps: calls.append((p, r)),
        )
        # Assert
        assert dispatched is False

    def test_remote_row_calls_handler_returns_true(
        self, fake_home, state_db, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="peer-host", a2a_port=18888)
        calls: list = []
        # Act
        dispatched = try_dispatch_remote(
            "alpha",
            "stop",
            self._peers_with("peer-host"),
            handler=lambda p, r, ps: calls.append((p, r["a2a_port"])),
        )
        # Assert
        assert dispatched is True and calls == [("peer-host", 18888)]

    def test_remote_peer_not_in_peers_raises_runtime_error(
        self, fake_home, state_db, env_save_restore
    ):
        # Arrange — row points at a peer that the lead's config.yaml
        # does NOT define. Must surface, not silently skip.
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="unknown-peer")

        # Act
        def _do() -> None:
            try_dispatch_remote(
                "alpha",
                "stop",
                self._peers_with("other-peer"),
                handler=lambda p, r, ps: None,
            )

        # Assert
        with pytest.raises(RuntimeError, match="NOT in"):
            _do()


# ---------------------------------------------------------------------------
# try_dispatch — concrete-host routing: local (no ssh) / remote (ssh argv) /
# unknown (fail loud). local + unknown reach no ssh; the remote path reuses
# the PATH-shim ssh + rsync (no live network) to assert the constructed argv.
# ``local_names`` is injected so routing is hermetic (no host_config read).
# ---------------------------------------------------------------------------


def _cfg_host(name: str, host) -> AgentConfig:
    """AgentConfig carrying a v3 ``spec.host`` pin (str / list / '')."""
    c = AgentConfig(name=name)
    c.hosts_spec = HostsSpec(host=host, hosts=[])
    return c


class TestTryDispatchClassification:
    def test_canonical_host_stays_local_and_skips_ssh(self, shim_bin, capsys):
        # Arrange — host == this machine; an ssh shim is present to prove it
        # is never invoked, and a peer map that would NOT rescue the name.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "ywata-note-win")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_alias_of_self_that_is_also_a_peer_stays_local(self, shim_bin, capsys):
        # Arrange — the machine is ALSO a peer (ssh: localhost); an alias
        # spelling must resolve local, never ssh-dispatch to itself.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "ywata-note-win")
        peers = {"ywata-note-win": PeerSpec(name="ywata-note-win", ssh="localhost")}
        # Act
        out = try_dispatch(
            cfg,
            "raw-short-name",
            peers,
            dry_run=False,
            force=False,
            local_names={"raw-short-name", "ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_absent_host_stays_local(self, shim_bin, capsys):
        # Arrange — host: local / absent normalizes to '' upstream.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_unknown_host_raises_naming_the_registered_peers(self, capsys):
        # Arrange — host is a typo: neither this machine nor a peer key. It
        # must FAIL LOUD with the registered-peer list (operator directive
        # 2026-07-10), never silently start on the wrong machine.
        cfg = _cfg_host("alpha", "spartn-gpgpu")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}

        # Act
        def _do() -> None:
            try_dispatch(
                cfg,
                "ywata-note-win",
                peers,
                dry_run=False,
                force=False,
                local_names={"ywata-note-win"},
            )

        # Assert
        with pytest.raises(RuntimeError, match="peer-host"):
            _do()

    def test_unknown_host_never_dispatches_ssh(self, shim_bin, capsys):
        # Arrange — an ssh shim is present; the unknown path must not touch
        # it (negative-safety: an unknown host raises BEFORE any ssh; the
        # raise itself is asserted by the sibling test and only absorbed
        # here so this test's single assert stays the ssh log).
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "spartn-gpgpu")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        try:
            try_dispatch(
                cfg,
                "ywata-note-win",
                peers,
                dry_run=False,
                force=False,
                local_names={"ywata-note-win"},
            )
        except RuntimeError:
            pass
        # Assert
        assert _ssh_invocations(shim_bin) == []

    def test_unknown_head_with_local_chain_tail_stays_local(self, shim_bin, capsys):
        # Arrange — fallback CHAIN whose tail names THIS machine: the
        # documented fallback-hosts semantics (singleton-skip accepts the
        # current host anywhere in the chain) must keep the local path
        # instead of failing loud on the dead head.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", ["dead-host", "ywata-note-win"])
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False

    def test_known_peer_dispatches_remote_with_expected_ssh_argv(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — host is a known peer distinct from the caller; PATH-shim
        # rsync (clean first-launch) + ssh (ok JSON) stand in for the network.
        # _dispatch_remote_start re-loads peers from the on-disk config for
        # build_ssh_argv, so register peer-host there too (real config.yaml).
        _write_peer_config(fake_home, env_save_restore, peer="peer-host")
        _install_rsync_shim(shim_bin, **_RK_OK)
        _install_ssh_shim(shim_bin, **_SK_OK)
        cfg = _cfg_host("alpha", "peer-host")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert — dispatched, and the ssh argv runs the peer-side start verb.
        ssh_calls = _ssh_invocations(shim_bin)
        assert out is True and ssh_calls[0][-6:] == [
            "sac",
            "agents",
            "start",
            "alpha",
            "--no-redispatch",
            "--json",
        ]
