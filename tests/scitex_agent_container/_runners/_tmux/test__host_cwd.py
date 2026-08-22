"""Unit tests for ``resolve_host_cwd`` — the tmux pane-cwd fallback.

Container-path workdirs (e.g. ``spec.workdir: /work`` backed only by an
in-container bind) are not host-creatable; ``TmuxManager.start`` used to
die on ``PermissionError: '/work'`` before the pane's ``cd`` ever ran
(2026-07-05, paper-scitex-clew capsule launch). These tests are hermetic
(no tmux binary, no mocks) per the runner test doctrine.

STX-TQ002 AAA markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._runners._tmux._host_cwd import resolve_host_cwd


def test_creatable_workdir_is_returned_unchanged(tmp_path: Path) -> None:
    # Arrange — a not-yet-existing but creatable nested path.
    target = tmp_path / "nested" / "pane-cwd"
    # Act
    result = resolve_host_cwd(str(target))
    # Assert
    assert result == str(target)


def test_creatable_workdir_is_created(tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "created-by-resolve"
    # Act
    resolve_host_cwd(str(target))
    # Assert
    assert target.is_dir()


def test_uncreatable_container_path_falls_back_to_tmp() -> None:
    # Arrange — /proc is a kernel fs: mkdir below it raises OSError for
    # any uid (root included), mirroring the unprivileged-host '/work'.
    container_only = "/proc/sac-test-does-not-exist/work"
    # Act
    result = resolve_host_cwd(container_only)
    # Assert
    assert result == "/tmp"


def test_uncreatable_path_warns_loudly(capsys) -> None:
    # Arrange — the WARN now goes through scitex-logging, whose stream handler
    # resolves sys.stderr at emit time (LazyStderrStreamHandler), so capsys
    # still sees it. Asserting on .err rather than .out is the POINT of the
    # change: this diagnostic used to be written to bare stdout on an agent
    # launch path where stdout is often attached to nothing.
    container_only = "/proc/sac-test-does-not-exist/work"
    # Act
    resolve_host_cwd(container_only)
    # Assert
    assert "not host-creatable" in capsys.readouterr().err
