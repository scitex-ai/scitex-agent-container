"""Unit tests for ``TmuxManager.session_activity`` — the pane-activity
probe step 4 of the TUI hedge wires into ``TuiSessionRuntime.is_running``
(see ``runtimes/tui_session.py`` + ``_runners/_tmux/tmux.py``).

The full subprocess success path (real tmux session, real
``tmux display -p '#{session_activity}'``) is exercised by the slow
real-binary smoke suite in ``tests/scitex_agent_container/runtimes/
test_tui_session_real_smoke.py`` which is skipped on hosts without
tmux. This module covers the fast, hermetic missing-session branch so
patch coverage stays above the codecov gate on CI containers that lack
tmux entirely.

STX-TQ002 AAA markers + STX-TQ007 one-assert. No mocks (no monkeypatch,
no MagicMock) — we hit real ``subprocess.run`` against ``tmux`` itself
when tmux is present; otherwise the suite skips.
"""

from __future__ import annotations

import shutil
import uuid

import pytest

from scitex_agent_container._runners._tmux.tmux import TmuxManager

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux binary not on PATH",
)


def test_session_activity_returns_none_for_absent_session() -> None:
    # Arrange — a session name guaranteed not to exist (uuid in name).
    bogus = f"tui-test-absent-{uuid.uuid4().hex[:8]}"
    # Act
    result = TmuxManager.session_activity(bogus)
    # Assert
    assert result is None
