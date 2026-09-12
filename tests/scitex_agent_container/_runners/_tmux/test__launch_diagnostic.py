from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from scitex_agent_container._runners._tmux.tmux import TmuxManager

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux binary not on PATH"
)


class _FailedTmux:
    def __call__(self, argv, **kwargs):  # noqa: ANN001
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(
            argv, returncode=1, stdout="tmux-out\n", stderr="duplicate session\n"
        )


def test_start_persists_exact_tmux_failure_before_returning_false(tmp_path: Path) -> None:
    # Arrange
    diagnostic = tmp_path / "tmux.start.json"
    # Act
    started = TmuxManager.start(
        "tui-broken",
        "false",
        str(tmp_path),
        session_env={"SAC_TMUX_START_DIAGNOSTICS_PATH": str(diagnostic)},
        runner=_FailedTmux(),
    )
    value = json.loads(diagnostic.read_text())
    # Assert
    assert (started, value) == (
        False,
        {
            "returncode": 1,
            "stdout": "tmux-out\n",
            "stderr": "duplicate session\n",
        },
    )


@requires_tmux
def test_start_captures_shell_failure_before_agent_exec(tmp_path: Path) -> None:
    # Arrange
    name = f"tui-pre-exec-{uuid.uuid4().hex[:8]}"
    boot_stderr = tmp_path / "boot.stderr.log"
    invocation = tmp_path / "tmux.start.json"
    # Act
    started = TmuxManager.start(
        name,
        "sleep 60",
        str(tmp_path),
        venv="/definitely/missing-sac-venv",
        session_env={
            "SAC_TMUX_BOOT_STDERR_PATH": str(boot_stderr),
            "SAC_TMUX_START_DIAGNOSTICS_PATH": str(invocation),
        },
    )
    value = json.loads(invocation.read_text())
    # Assert
    assert (
        started,
        value["returncode"],
        "No such file or directory" in boot_stderr.read_text(),
        TmuxManager.exists(name),
    ) == (False, 0, True, False)
