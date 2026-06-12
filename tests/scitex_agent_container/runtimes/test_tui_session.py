"""Tests for ``runtimes/tui_session.TuiSessionRuntime`` (skeleton).

The TUI runner is a skeleton landed alongside the ``spec.runtime``
field plumbing. The follow-up PR wires the tmux session + tui-alive
health hook + inner-process restart adapter + auto-compact (the four
TUI-stability risks named in the module docstring).

Until then, every entry point raises ``NotImplementedError`` so a
misconfigured ``runtime: tui`` spec fails LOUDLY at dispatch instead
of silently doing nothing — operator directive 12847 (fail-loud, no
silent fallback).

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime


def _stub_config():
    return SimpleNamespace(name="alpha", runtime="tui")


# ---------------------------------------------------------------------------
# Every RuntimeBase entry point raises NotImplementedError loudly
# ---------------------------------------------------------------------------


def test_start_raises_not_implemented_error():
    # Arrange
    runtime = TuiSessionRuntime()
    config = _stub_config()
    raised: BaseException | None = None
    # Act
    try:
        runtime.start(config)
    except (
        NotImplementedError
    ) as exc:  # stx-allow: test-capture (reason: STX-TQ002; skeleton contract.)
        raised = exc
    # Assert
    assert isinstance(raised, NotImplementedError)


def test_stop_raises_not_implemented_error():
    # Arrange
    runtime = TuiSessionRuntime()
    config = _stub_config()
    raised: BaseException | None = None
    # Act
    try:
        runtime.stop(config)
    except NotImplementedError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, NotImplementedError)


def test_is_running_raises_not_implemented_error():
    # Arrange
    runtime = TuiSessionRuntime()
    config = _stub_config()
    raised: BaseException | None = None
    # Act
    try:
        runtime.is_running(config)
    except NotImplementedError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, NotImplementedError)


def test_logs_raises_not_implemented_error():
    # Arrange
    runtime = TuiSessionRuntime()
    config = _stub_config()
    raised: BaseException | None = None
    # Act
    try:
        runtime.logs(config)
    except NotImplementedError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, NotImplementedError)


def test_skeleton_error_message_mentions_follow_up_pr():
    # Arrange — the message must point operators / readers at the
    # follow-up PR so a misconfig has a clear next step.
    runtime = TuiSessionRuntime()
    config = _stub_config()
    raised: BaseException | None = None
    # Act
    try:
        runtime.start(config)
    except NotImplementedError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and "follow-up" in str(raised).lower()
