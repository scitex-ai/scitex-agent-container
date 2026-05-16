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

from scitex_agent_container.cli_pkg.lifecycle._dispatch import (
    _dispatch_remote_start,
)

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
def state_db(fake_home: Path, env_save_restore) -> Path:
    """Redirect state.db to a tmp path under fake_home."""
    db = fake_home / "state.db"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(db))
    return db


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
