"""Tests for the credential-refresh single-flight boot gate.

Card sac-multi-start-queue-oauth (2026-06-25). The gate serializes the
OAuth-refresh boot window across concurrent brokered spawns so they don't race
the shared ~/.claude/.credentials.json token rotation.

Discipline: AAA markers each on their own line; one literal ``assert`` per
test; real filesystem + real fcntl flock fixtures (``tmp_path`` + threads), no
mocks.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from scitex_agent_container._listen._credential_refresh_lock import (
    _refresh_imminent,
    credential_boot_gate,
    credential_lock_path,
    gate_settings_from_env,
    run_brokered_launch,
)


@pytest.fixture
def settle_disabled_env():
    """Set SAC_CREDS_SETTLE_SECONDS=0 on the real environment, restore on exit.

    A yield fixture (not ``monkeypatch``) so the gate reads the real env var
    exactly as production does."""
    key = "SAC_CREDS_SETTLE_SECONDS"
    prev = os.environ.get(key)
    os.environ[key] = "0"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def test_credential_lock_path_is_scoped_under_lock_dir(tmp_path: Path) -> None:
    # Arrange
    lock_dir = tmp_path / "runtime"
    # Act
    path = credential_lock_path(lock_dir)
    # Assert
    assert path == lock_dir / "creds-refresh.lock"


def test_refresh_imminent_unknown_expiry_is_false() -> None:
    # Arrange — unknown expiry: nothing observable to settle on, so the gate
    # must not block (the flock still serializes the brief launch window).
    # Act
    result = _refresh_imminent(None, now_ms=0.0, window_ms=300_000.0)
    # Assert
    assert result is False


def test_refresh_imminent_far_future_is_false() -> None:
    # Arrange — token expires well outside the imminence window.
    # Act
    result = _refresh_imminent(600_000, now_ms=0.0, window_ms=300_000.0)
    # Assert
    assert result is False


def test_refresh_imminent_near_expiry_is_true() -> None:
    # Arrange — token expires inside the imminence window.
    # Act
    result = _refresh_imminent(60_000, now_ms=0.0, window_ms=300_000.0)
    # Assert
    assert result is True


def test_gate_is_noop_when_settle_disabled(tmp_path: Path) -> None:
    # Arrange — settle_seconds <= 0 disables the gate entirely.
    lock_dir = tmp_path / "runtime"
    # Act
    with credential_boot_gate(lock_dir=lock_dir, settle_seconds=0.0):
        pass
    # Assert — no lock file is created when the gate is a no-op.
    assert not credential_lock_path(lock_dir).exists()


def test_gate_serializes_concurrent_holders(tmp_path: Path) -> None:
    # Arrange — three threads contend for the same lock dir; the flock must
    # admit only one at a time. imminent_window_seconds=0 avoids the settle
    # wait when a real (non-expired) cred file is present; the per-body sleep
    # creates the contention window.
    lock_dir = tmp_path / "runtime"
    inside: list[int] = []
    violations: list[int] = []

    def worker() -> None:
        with credential_boot_gate(
            lock_dir=lock_dir, settle_seconds=0.05, imminent_window_seconds=0.0
        ):
            inside.append(1)
            if len(inside) > 1:
                violations.append(1)
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Assert — never two holders inside the gate simultaneously.
    assert violations == []


def test_gate_settings_from_env_parses_settle_override(settle_disabled_env) -> None:
    # Arrange — settle_disabled_env sets SAC_CREDS_SETTLE_SECONDS=0 for real.
    # Act
    _lock_dir, settle, _window = gate_settings_from_env()
    # Assert
    assert settle == 0.0


@pytest.mark.asyncio
async def test_run_brokered_launch_foreground_runs_argv() -> None:
    # Arrange — foreground bypasses the gate but must still run the argv.
    argv = [sys.executable, "-c", "import sys; sys.exit(7)"]
    # Act
    proc = await run_brokered_launch(
        argv, dict(os.environ), foreground=True, one_shot=False
    )
    # Assert
    assert proc.returncode == 7


@pytest.mark.asyncio
async def test_run_brokered_launch_background_runs_argv(settle_disabled_env) -> None:
    # Arrange — settle_disabled_env disables the gate so the background path
    # just runs the argv (no flock / settle).
    argv = [sys.executable, "-c", "import sys; sys.exit(5)"]
    # Act
    proc = await run_brokered_launch(
        argv, dict(os.environ), foreground=False, one_shot=False
    )
    # Assert
    assert proc.returncode == 5
