"""Watchers the supervisor runs beside ONE sealed container.

* :class:`SizeWatchdog` — ``run.size`` over the whole writable run dir;
* :class:`Deadline` — a wall-clock limit (``sac sandbox exec --timeout``);
* :func:`start_tee` — copies the container's stdout/stderr to the caller
  AND to ``out/stdout.log`` / ``out/stderr.log`` (plain-command runs).

Each terminates the container the same way: SIGTERM, then SIGKILL after a
grace period if it is still running.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "Deadline",
    "SizeWatchdog",
    "run_dir_bytes",
    "start_tee",
]

#: How often the run.size watchdog measures the run dir, and the grace
#: between a watcher's SIGTERM and SIGKILL.
SIZE_POLL_S = 5.0
SIZE_KILL_GRACE_S = 10.0
KILL_GRACE_S = SIZE_KILL_GRACE_S


def run_dir_bytes(run_dir: Path, exclude: tuple[str, ...] = ("out", "secrets")) -> int:
    """Allocated bytes under ``run_dir`` (overlay, $HOME, /tmp, /uvwork, ...)."""
    total = 0
    for entry in os.scandir(run_dir):
        if entry.name in exclude:
            continue
        if not entry.is_dir(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_blocks * 512
            continue
        for dirpath, _dirs, files in os.walk(entry.path):
            for fn in files:
                try:
                    total += os.lstat(os.path.join(dirpath, fn)).st_blocks * 512
                except OSError:
                    continue
    return total


def _terminate(child, stop_evt: threading.Event, grace_s: float) -> None:
    """SIGTERM ``child``; SIGKILL it if it outlives ``grace_s``."""
    child.terminate()
    if stop_evt.wait(grace_s) is False and child.poll() is None:
        child.kill()


class SizeWatchdog(threading.Thread):
    """Enforces ``run.size`` over the WHOLE writable run dir.

    A FUSE ext3 overlay image would cap only the rootfs layer and is slow
    on loaded hosts; measuring the run dir caps $HOME, /tmp and /uvwork too.
    On overflow the container is terminated and the reason recorded.
    """

    def __init__(
        self, run_dir: Path, limit_bytes: int, child, *, poll_s: float = SIZE_POLL_S
    ):
        super().__init__(name="sac-size-watchdog", daemon=True)
        self.run_dir, self.limit, self.child, self.poll_s = (
            run_dir,
            limit_bytes,
            child,
            poll_s,
        )
        self.exceeded_at_bytes: int | None = None
        self.peak = 0
        self.stop_evt = threading.Event()

    def run(self) -> None:
        while not self.stop_evt.wait(self.poll_s):
            used = run_dir_bytes(self.run_dir)
            self.peak = max(self.peak, used)
            if used > self.limit:
                self.exceeded_at_bytes = used
                _terminate(self.child, self.stop_evt, SIZE_KILL_GRACE_S)
                return


class Deadline(threading.Thread):
    """Terminates ``child`` once ``seconds`` of wall-clock time have passed.

    ``fired`` tells the supervisor the exit was the deadline's doing; set
    ``stop_evt`` when the child exits on its own.
    """

    def __init__(self, seconds: float, child, *, grace_s: float = KILL_GRACE_S):
        super().__init__(name="sac-deadline", daemon=True)
        self.seconds, self.child, self.grace_s = float(seconds), child, grace_s
        self.fired = False
        self.stop_evt = threading.Event()

    def run(self) -> None:
        if self.stop_evt.wait(self.seconds):
            return
        self.fired = True
        _terminate(self.child, self.stop_evt, self.grace_s)


def _pump(src: BinaryIO, sinks: tuple[BinaryIO, ...]) -> None:
    fd = src.fileno()
    while True:
        try:
            chunk = os.read(fd, 1 << 16)
        except OSError:
            break
        if not chunk:
            break
        for sink in sinks:
            try:
                sink.write(chunk)
                sink.flush()
            except (OSError, ValueError):
                continue
    src.close()
    for sink in sinks[1:]:  # the log files; the caller's stream stays open
        sink.close()


def start_tee(src: BinaryIO, caller: BinaryIO, log_path: Path) -> threading.Thread:
    """Copy ``src`` (a child pipe) to ``caller`` and to ``log_path`` until EOF.

    Bytes are forwarded as they arrive (no line buffering), so the caller
    sees the command's output live; the log is the run's durable copy.
    """
    log = open(log_path, "wb")  # noqa: SIM115 - closed by the pump at EOF
    t = threading.Thread(
        target=_pump, args=(src, (caller, log)), name="sac-tee", daemon=True
    )
    t.start()
    return t
