"""Bot-token singleton via flock + stale-PID recovery.

Only one process per bot token may long-poll Telegram (the API returns 409
Conflict otherwise). We enforce that with an advisory ``fcntl.flock`` over a
per-token file at::

    ~/.scitex/agent-container/runtime/telegram/<token-hash>.lock

The file holds the holder's PID. On acquire we check ``kill(pid, 0)``: if
the recorded PID is dead, the lock is stale — we delete the file and retry
once. This is the failure-mode the standalone telegrammer suffers from
(crashed PID leaves a dangling file; future starts block until manual
``rm``).

The path uses ``runtime/`` (not ``containers/``) per the Phase 2 task
description — telegram singletons are a runtime concern, not container
state.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def lock_dir() -> Path:
    """Return the runtime lock directory, creating it if missing."""
    p = Path.home() / ".scitex" / "agent-container" / "runtime" / "telegram"
    p.mkdir(parents=True, exist_ok=True)
    return p


def lock_path_for(bot_token: str) -> Path:
    """Per-token lock file path. Hash the token so it never lands on disk."""
    digest = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:32]
    return lock_dir() / f"{digest}.lock"


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live process on this host.

    ``kill(pid, 0)`` sends signal 0 — no actual signal is delivered, but
    the kernel still checks whether the target exists. ``ProcessLookupError``
    (ESRCH) means it does not; ``PermissionError`` means it exists but the
    caller doesn't own it (still alive, just foreign).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # stx-allow: fallback (reason: best-effort liveness check; treat unknown errno as "alive")
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


@dataclass
class _LockHandle:
    fd: int
    path: Path


class TelegramLockError(RuntimeError):
    """Raised when the lock is held by another live process."""


class TelegramBridgeLock:
    """Context manager around the per-token flock.

    Stale-recovery: if ``flock`` fails because the file is held but the
    recorded PID is not alive, we delete the file and retry ONCE. A second
    failure is a real conflict — raise ``TelegramLockError``.
    """

    def __init__(self, bot_token: str) -> None:
        self._path = lock_path_for(bot_token)
        self._handle: _LockHandle | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _try_acquire(self) -> _LockHandle | None:
        """Open and try to acquire. Returns handle on success, None on
        ``EWOULDBLOCK``. Any other error propagates."""
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        # Record our pid for the next acquirer's stale check.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(fd)
        except (
            OSError
        ):  # pragma: no cover  # stx-allow: fallback (reason: fsync best-effort)
            pass
        return _LockHandle(fd=fd, path=self._path)

    def _read_recorded_pid(self) -> int | None:
        try:
            txt = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not txt:
            return None
        try:
            return int(txt.splitlines()[0])
        except ValueError:
            return None

    def acquire(self) -> None:
        """Acquire the lock; perform one stale-recovery retry."""
        if self._handle is not None:
            return  # idempotent: already held
        handle = self._try_acquire()
        if handle is not None:
            self._handle = handle
            return
        # Locked — peek at recorded PID
        pid = self._read_recorded_pid()
        if pid is not None and not _pid_alive(pid):
            log.warning(
                "telegram lock %s held by dead pid %s; reclaiming",
                self._path,
                pid,
            )
            try:
                self._path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover  # stx-allow: fallback (reason: race with another reclaimer)
                pass
            handle = self._try_acquire()
            if handle is not None:
                self._handle = handle
                return
        raise TelegramLockError(
            f"telegram bridge lock {self._path} is held by another live "
            f"process (recorded pid={pid}); refusing to start a second poller"
        )

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover  # stx-allow: fallback (reason: descriptor may have been closed)
            pass
        try:
            os.close(self._handle.fd)
        except (
            OSError
        ):  # pragma: no cover  # stx-allow: fallback (reason: idempotent close)
            pass
        self._handle = None

    def __enter__(self) -> "TelegramBridgeLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = [
    "TelegramBridgeLock",
    "TelegramLockError",
    "lock_dir",
    "lock_path_for",
]
