"""``TmuxManager.start`` must never leak a legacy scitex-todo env var
into the tmux pane environment (INCIDENT 2026-07-05).

``subprocess.run(argv, check=False)`` with no ``env=`` kwarg inherits
the CALLING process's full ambient environment into the tmux pane —
the same full-ambient-env-passthrough class documented for the
SDK-Popen exec site in ``runtimes/_apptainer_runtime.py``. A stale
``SCITEX_TODO_AGENT`` export left in this test process's env (as a
stand-in for an operator's launching shell that never migrated off
the pre-scitex-todo-0.7.30 name) must be scrubbed before the tmux
pane's shell writes its own ``env`` snapshot — production already
writes that snapshot to ``/tmp/sac-tui-env-<session>.txt`` right
before ``exec``ing the real command (lead a2a 4303f855), which this
test reads back as ground truth instead of inspecting the Python-side
dict sac builds.

Real ``tmux`` binary + real ``subprocess.run`` — no mocks. Skips when
``tmux`` is not on PATH (matches ``test_tmux_session_activity.py``).

STX-TQ002 AAA markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._runners._tmux.tmux import TmuxManager

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux binary not on PATH",
)


@pytest.fixture
def stale_legacy_env() -> Iterator[None]:
    """Set a stale pre-rename ``SCITEX_TODO_AGENT`` in THIS process's
    ambient env (what ``subprocess.run`` inherits absent ``env=``),
    auto-restoring afterwards."""
    prev = os.environ.get("SCITEX_TODO_AGENT")
    os.environ["SCITEX_TODO_AGENT"] = "stale-legacy-value"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("SCITEX_TODO_AGENT", None)
        else:
            os.environ["SCITEX_TODO_AGENT"] = prev


def test_start_scrubs_legacy_env_from_tmux_pane(
    tmp_path: Path, stale_legacy_env: None
) -> None:
    # Arrange
    session_name = f"tui-test-legacy-scrub-{uuid.uuid4().hex[:8]}"
    workdir = tmp_path / "wd"
    workdir.mkdir()
    snapshot_path = Path(f"/tmp/sac-tui-env-{session_name}.txt")
    try:
        # Act — a plain (non-apptainer) command; production writes the
        # env snapshot unconditionally before ``exec``ing it.
        TmuxManager.start(session_name, "sleep 5", str(workdir))
        deadline = time.time() + 5
        while not snapshot_path.exists() and time.time() < deadline:
            time.sleep(0.1)
        dumped = snapshot_path.read_text() if snapshot_path.exists() else ""
        # Assert
        assert "SCITEX_TODO_AGENT=" not in dumped
    finally:
        TmuxManager.stop(session_name)
        snapshot_path.unlink(missing_ok=True)
