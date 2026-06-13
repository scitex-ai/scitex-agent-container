"""Smoke surface for ``_runners/_tmux/auto/daemon.py``.

The auto-accept daemon loop is dormant behind the TUI hedge flag
(``spec.runtime: tui``) and is exercised end-to-end by the follow-up
integration PR. Until then these tests pin the public pidfile surface
+ run_daemon callable using real on-disk paths (no mocks). One assert
per test, AAA markers each on own line per STX-TQ002/007.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._runners._tmux.auto import daemon as D


@pytest.fixture
def isolated_pid_dir(tmp_path: Path) -> Iterator[Path]:
    """Per-test pidfile dir under tmp_path (no monkeypatch).

    ``daemon._PID_DIR`` is read once at module import; we overwrite
    the module-level attribute directly via setattr (a real
    attribute assignment, not a mock) and restore the original in
    teardown. Each test sees a fresh on-disk dir under tmp_path so
    parallel runs don.t collide.
    """
    new_dir = tmp_path / "registry"
    new_dir.mkdir(parents=True, exist_ok=True)
    saved = D._PID_DIR
    D._PID_DIR = new_dir
    try:
        yield new_dir
    finally:
        D._PID_DIR = saved


def test_run_daemon_callable_exists_on_module_surface() -> None:
    # Arrange
    module = D
    # Act
    obj = getattr(module, "run_daemon", None)
    # Assert
    assert callable(obj)


def test_write_pid_creates_file_with_current_process_pid(
    isolated_pid_dir: Path,
) -> None:
    # Arrange
    name = "alpha"
    # Act
    D.write_pid(name)
    # Assert
    assert (isolated_pid_dir / f"auto-accept-{name}.pid").read_text().strip() == str(
        os.getpid()
    )


def test_read_pid_returns_value_written_by_write_pid(
    isolated_pid_dir: Path,
) -> None:
    # Arrange
    name = "beta"
    D.write_pid(name)
    # Act
    got = D.read_pid(name)
    # Assert
    assert got == os.getpid()


def test_clear_pid_removes_the_pidfile_for_named_agent(
    isolated_pid_dir: Path,
) -> None:
    # Arrange
    name = "gamma"
    D.write_pid(name)
    # Act
    D.clear_pid(name)
    # Assert
    assert (isolated_pid_dir / f"auto-accept-{name}.pid").exists() is False
