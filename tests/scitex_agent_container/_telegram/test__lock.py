"""Tests for the flock singleton + stale-PID recovery.

Each test isolates the lock file by overriding the ``HOME`` env var for
its duration — ``Path.home()`` reads ``HOME``, so the lock dir lands
under the per-test ``tmp_path``. No ``monkeypatch``; the override is a
plain dict-mutation with explicit teardown.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container._telegram import _lock
from scitex_agent_container._telegram._lock import (
    TelegramBridgeLock,
    TelegramLockError,
    lock_path_for,
)


@contextlib.contextmanager
def _home_override(home: Path) -> Iterator[None]:
    sentinel = object()
    prev = os.environ.get("HOME", sentinel)
    try:
        os.environ["HOME"] = str(home)
        yield
    finally:
        if prev is sentinel:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev  # type: ignore[assignment]


@pytest.fixture()
def isolated_home(tmp_path) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    with _home_override(home):
        yield home


def _unique_token() -> str:
    """Each test uses a fresh token so parallel runs don't collide on the
    same lock file inside the isolated home."""
    return f"tkn-{uuid.uuid4().hex[:12]}"


def test_lock_path_for_uses_runtime_telegram_dir(isolated_home) -> None:
    # Arrange
    token = _unique_token()

    # Act
    p = lock_path_for(token)

    # Assert
    assert "runtime/telegram/" in str(p)


def test_lock_path_for_hashes_token_into_filename(isolated_home) -> None:
    # Arrange
    token = "secret-token-leak-canary"

    # Act
    p = lock_path_for(token)

    # Assert
    assert token not in str(p)


def test_acquire_creates_lock_file(isolated_home) -> None:
    # Arrange
    lock = TelegramBridgeLock(_unique_token())

    # Act
    lock.acquire()
    exists = lock.path.is_file()
    lock.release()

    # Assert
    assert exists is True


def test_acquire_writes_holder_pid(isolated_home) -> None:
    # Arrange
    lock = TelegramBridgeLock(_unique_token())

    # Act
    lock.acquire()
    recorded = lock.path.read_text(encoding="utf-8").strip()
    lock.release()

    # Assert
    assert recorded == str(os.getpid())


def test_acquire_is_idempotent(isolated_home) -> None:
    # Arrange
    lock = TelegramBridgeLock(_unique_token())
    lock.acquire()

    # Act
    second_call_raised = False
    try:
        lock.acquire()
    except Exception:  # stx-allow: fallback (reason: any error here is a regression we want to surface as an assertion miss, not as a test error)
        second_call_raised = True
    lock.release()

    # Assert
    assert second_call_raised is False


def test_release_without_prior_acquire_is_safe(isolated_home) -> None:
    # Arrange
    lock = TelegramBridgeLock(_unique_token())
    raised = False

    # Act
    try:
        lock.release()
    except Exception:  # stx-allow: fallback (reason: release must be idempotent)
        raised = True

    # Assert
    assert raised is False


@pytest.fixture()
def child_holder(isolated_home):
    """Spawn a child that takes a lock then sleeps; tear it down on exit."""
    token = _unique_token()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from scitex_agent_container._telegram._lock import "
                "TelegramBridgeLock; import time, sys; "
                f"l = TelegramBridgeLock({token!r}); l.acquire(); "
                "sys.stdout.write('locked\\n'); sys.stdout.flush(); "
                "time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        env={**os.environ},
    )
    try:
        assert child.stdout is not None
        readiness = child.stdout.readline()
        if readiness.strip() != b"locked":
            child.terminate()
            child.wait(timeout=5)
            pytest.fail("child failed to acquire lock")
        yield token
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()


def test_second_live_holder_raises_lock_error(child_holder) -> None:
    # Arrange
    token = child_holder

    # Act
    # (raising call inside pytest.raises)

    # Assert
    with pytest.raises(TelegramLockError):
        TelegramBridgeLock(token).acquire()


def test_stale_pid_lock_is_reclaimed(isolated_home) -> None:
    # Arrange: simulate a crashed prior holder by writing a dead PID.
    token = _unique_token()
    path = lock_path_for(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = 2**22
    path.write_text(f"{dead_pid}\n", encoding="utf-8")

    # Act
    lock = TelegramBridgeLock(token)
    lock.acquire()
    recorded = path.read_text(encoding="utf-8").strip()
    lock.release()

    # Assert
    assert recorded == str(os.getpid())


def test_pid_alive_reports_self_as_alive() -> None:
    # Arrange
    pid = os.getpid()

    # Act
    alive = _lock._pid_alive(pid)

    # Assert
    assert alive is True


def test_pid_alive_reports_zero_as_dead() -> None:
    # Arrange
    pid = 0

    # Act
    alive = _lock._pid_alive(pid)

    # Assert
    assert alive is False


def test_pid_alive_reports_huge_pid_as_dead() -> None:
    # Arrange
    pid = 2**22

    # Act
    alive = _lock._pid_alive(pid)

    # Assert
    assert alive is False
