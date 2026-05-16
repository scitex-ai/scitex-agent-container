"""Tests for cli_pkg.lifecycle._dispatch._dispatch_remote_start (step 3b).

See ``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``.
Eight scenarios cover the drift-check / rsync surface: first-launch,
drift-without-force, drift-with-force, no-changes, dry_run=True, dry-run
rsync failure, real rsync failure, and missing local spec dir.

No-mocks: real ``subprocess.run`` against a PATH-prepended fake
``rsync`` that branches on the dry-run ``-acvn`` short-opt and records
each argv to a JSON-lines log. Conforms to STX-TQ002 (AAA markers),
STX-TQ003 (descriptive names), STX-TQ007 (one assert per test). Per
PS-204 §2 dispatch tests live HERE, not in ``test__common.py``.
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
# Helpers: dual-behavior rsync shim (dry-run vs real differentiation).
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
    """Install a Python-script ``rsync`` shim that branches on ``-acvn``.

    The shim appends its argv (JSON list) to ``$bin_dir/rsync.argv.jsonl``
    and emits different stdout / stderr / exit codes depending on whether
    a bundled short-opt blob containing ``n`` (like ``-acvn``) appears in
    argv. The dispatcher always passes ``-acvn`` for dry-run and ``-acv``
    for real, so we look for an argv token that starts with a single
    ``-`` and contains ``n``.
    """
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
        f"dry_stdout = {json.dumps(dry_stdout)}\n"
        f"dry_stderr = {json.dumps(dry_stderr)}\n"
        f"dry_exit = {int(dry_exit)}\n"
        f"real_stdout = {json.dumps(real_stdout)}\n"
        f"real_stderr = {json.dumps(real_stderr)}\n"
        f"real_exit = {int(real_exit)}\n"
        "if is_dry:\n"
        "    sys.stdout.write(dry_stdout)\n"
        "    sys.stderr.write(dry_stderr)\n"
        "    sys.exit(dry_exit)\n"
        "else:\n"
        "    sys.stdout.write(real_stdout)\n"
        "    sys.stderr.write(real_stderr)\n"
        "    sys.exit(real_exit)\n"
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


def _rsync_dry_count(bin_dir: Path) -> int:
    return sum(1 for argv in _rsync_invocations(bin_dir) if _is_dry_run_argv(argv))


def _rsync_real_count(bin_dir: Path) -> int:
    return sum(1 for argv in _rsync_invocations(bin_dir) if not _is_dry_run_argv(argv))


# ---------------------------------------------------------------------------
# Shared fixtures and scenario builder.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore):
    """Redirect HOME so Path.home() returns tmp_path."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def spec_dir(fake_home: Path) -> Path:
    """Create a populated spec dir at ``~/.scitex/agent-container/agents/alpha``."""
    d = fake_home / ".scitex" / "agent-container" / "agents" / "alpha"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: alpha\n")
    return d


@pytest.fixture
def shim_bin(tmp_path: Path, env_save_restore) -> Path:
    """Prepend a fresh bin dir to PATH for rsync shim installation."""
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir()
    saved_path = os.environ.get("PATH", "")
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{saved_path}")
    return bin_dir


@dataclass
class _Scenario:
    """Outcome of one ``_dispatch_remote_start`` invocation under a shim."""

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
    shim_kwargs: dict[str, Any],
    name: str = "alpha",
    peer: str = "peer-host",
    dry_run: bool = False,
    force: bool = False,
) -> _Scenario:
    """Install the shim and invoke ``_dispatch_remote_start`` once,
    capturing the raised exception (if any), the return value, and
    the captured stdout/stderr.
    """
    _install_rsync_shim(shim_bin, **shim_kwargs)
    scen = _Scenario(bin_dir=shim_bin)
    try:
        scen.returned = _dispatch_remote_start(name, peer, dry_run=dry_run, force=force)
    except BaseException as exc:
        scen.raised = exc
    captured = capsys.readouterr()
    scen.captured_stdout = captured.out
    scen.captured_stderr = captured.err
    return scen


# Reusable arrangement payloads (named so test bodies stay short).
_FIRST_LAUNCH_OUTPUT = ">f+++++++++ spec.yaml\ncd+++++++++ overlays/\n"
_DRIFT_OUTPUT = ">f.st...... spec.yaml\n>f+++++++++ NEWFILE.txt\n"


# ---------------------------------------------------------------------------
# 1. First-launch path: drift-check is all-plus → step-4 NotImplementedError,
#    real rsync was invoked.
# ---------------------------------------------------------------------------


class TestDispatchFirstLaunchProceedsToStep4:
    def test_first_launch_raises_not_implemented_error(
        self, spec_dir, shim_bin, capsys
    ):
        # Arrange
        shim_kwargs = dict(dry_stdout=_FIRST_LAUNCH_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert isinstance(scen.raised, NotImplementedError)

    def test_first_launch_message_mentions_step_four(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_FIRST_LAUNCH_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "step 4" in scen.message

    def test_first_launch_invokes_real_rsync_once(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_FIRST_LAUNCH_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert scen.real_count == 1


# ---------------------------------------------------------------------------
# 2. Drift without --force: blocks with RuntimeError, no real rsync.
# ---------------------------------------------------------------------------


class TestDispatchDriftBlocksWithoutForce:
    def test_drift_without_force_raises_runtime_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_drift_message_mentions_spec_drift(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "Spec drift" in scen.message

    def test_drift_does_not_invoke_real_rsync(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert scen.real_count == 0


# ---------------------------------------------------------------------------
# 3. Drift with --force=True: drift overridden → step-4 NotImplementedError +
#    real rsync invoked.
# ---------------------------------------------------------------------------


class TestDispatchDriftOverriddenByForce:
    def test_force_drift_raises_not_implemented_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, force=True)
        # Assert
        assert isinstance(scen.raised, NotImplementedError)

    def test_force_drift_message_mentions_step_four(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, force=True)
        # Assert
        assert "step 4" in scen.message

    def test_force_drift_invokes_real_rsync_once(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=_DRIFT_OUTPUT, dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, force=True)
        # Assert
        assert scen.real_count == 1


# ---------------------------------------------------------------------------
# 4. No-changes dry-run: real rsync still runs (current spec), then step-4.
# ---------------------------------------------------------------------------


class TestDispatchNoChangesProceedsToStep4:
    def test_no_changes_raises_not_implemented_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout="", dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert isinstance(scen.raised, NotImplementedError)

    def test_no_changes_message_mentions_step_four(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout="", dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "step 4" in scen.message

    def test_no_changes_still_invokes_real_rsync_once(self, spec_dir, shim_bin, capsys):
        # Per current spec the actual rsync IS called even when the
        # dry-run reported zero changes — only ``dry_run=True`` short-
        # circuits the real rsync.
        # Arrange
        shim_kwargs = dict(dry_stdout="", dry_exit=0, real_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert scen.real_count == 1


# ---------------------------------------------------------------------------
# 5. dry_run=True: echoes plan, returns 0, never invokes real rsync.
# ---------------------------------------------------------------------------


class TestDispatchDryRunMode:
    def test_dry_run_mode_does_not_raise(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, dry_run=True)
        # Assert
        assert scen.raised is None

    def test_dry_run_mode_returns_zero_exit(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, dry_run=True)
        # Assert
        assert scen.returned == 0

    def test_dry_run_mode_prints_dispatch_marker(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, dry_run=True)
        # Assert
        assert "[dispatch] dry-run" in scen.captured_stdout

    def test_dry_run_mode_prints_change_count(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, dry_run=True)
        # Assert
        assert "1 file change(s)" in scen.captured_stdout

    def test_dry_run_mode_skips_real_rsync(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_stdout=">f+++++++++ spec.yaml\n", dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, dry_run=True)
        # Assert
        assert scen.real_count == 0


# ---------------------------------------------------------------------------
# 6. rsync --dry-run failure: RuntimeError with "rsync --dry-run failed".
# ---------------------------------------------------------------------------


class TestDispatchDryRunFailure:
    def test_dry_run_failure_raises_runtime_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="", dry_stderr="ssh: host unreachable\n", dry_exit=255
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dry_run_failure_message_identifies_phase(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="", dry_stderr="ssh: host unreachable\n", dry_exit=255
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "rsync --dry-run failed" in scen.message

    def test_dry_run_failure_skips_real_rsync(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="", dry_stderr="ssh: host unreachable\n", dry_exit=255
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert scen.real_count == 0


# ---------------------------------------------------------------------------
# 7. Real rsync failure: clean dry-run, then RuntimeError "rsync failed".
# ---------------------------------------------------------------------------


class TestDispatchRealRsyncFailure:
    def test_real_rsync_failure_raises_runtime_error(self, spec_dir, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="",
            dry_exit=0,
            real_stderr="rsync: write error: broken pipe\n",
            real_exit=12,
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_real_rsync_failure_message_mentions_rsync_failed(
        self, spec_dir, shim_bin, capsys
    ):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="",
            dry_exit=0,
            real_stderr="rsync: write error: broken pipe\n",
            real_exit=12,
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "rsync failed" in scen.message

    def test_real_rsync_failure_message_excludes_dry_run_phase(
        self, spec_dir, shim_bin, capsys
    ):
        # Disambiguate from the dry-run failure path.
        # Arrange
        shim_kwargs = dict(
            dry_stdout="",
            dry_exit=0,
            real_stderr="rsync: write error: broken pipe\n",
            real_exit=12,
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert "rsync --dry-run failed" not in scen.message

    def test_real_rsync_failure_invokes_real_rsync_once(
        self, spec_dir, shim_bin, capsys
    ):
        # Arrange
        shim_kwargs = dict(
            dry_stdout="",
            dry_exit=0,
            real_stderr="rsync: write error: broken pipe\n",
            real_exit=12,
        )
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs)
        # Assert
        assert scen.real_count == 1


# ---------------------------------------------------------------------------
# 8. Missing local spec dir: FileNotFoundError, no rsync invoked at all.
# ---------------------------------------------------------------------------


class TestDispatchMissingSpecDir:
    def test_missing_spec_dir_raises_file_not_found(self, fake_home, shim_bin, capsys):
        # Arrange — fake_home redirects HOME but NO spec dir is created.
        shim_kwargs = dict(dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, name="ghost")
        # Assert
        assert isinstance(scen.raised, FileNotFoundError)

    def test_missing_spec_dir_message_names_problem(self, fake_home, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, name="ghost")
        # Assert
        assert "Spec dir for" in scen.message

    def test_missing_spec_dir_skips_dry_run_rsync(self, fake_home, shim_bin, capsys):
        # Arrange
        shim_kwargs = dict(dry_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, shim_kwargs=shim_kwargs, name="ghost")
        # Assert
        assert scen.dry_count == 0
