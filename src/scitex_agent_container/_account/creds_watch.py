"""Watch the live Claude credential and auto-sync on every change.

This is the "moment I log in → auto-saved" mechanism behind
``sac accounts watch-live``. It watches ``~/.claude/.credentials.json``
and runs the :func:`creds_sync.sync_live` engine whenever the file
changes (a ``claude /login`` rewrite, or claude-code's ~1h OAuth
refresh).

Two watch backends, picked at runtime:

* **inotify** via the ``inotifywait`` binary when it is on ``$PATH`` —
  event-driven, zero idle CPU.
* **poll** fallback otherwise — stat the file's ``mtime``/``size`` on a
  short interval.

Both call the same engine, so behaviour is identical; only the
wake-up mechanism differs. Each sync attempt is logged (stderr by
default, or a log file under
``~/.scitex/agent-container/runtime/logs/``). A
:class:`creds_sync.LiveCredInvalidError` during a sync is logged and
the watcher keeps running — a transient expired/mid-rewrite state must
not kill the long-lived daemon.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from .creds_sync import LiveCredInvalidError, sync_live

# Poll cadence for the fallback loop. Short enough that a /login is
# mirrored within a couple seconds; long enough to be near-zero CPU.
DEFAULT_POLL_INTERVAL_S = 2.0


def default_log_path(home: Path | None = None) -> Path:
    """Return the canonical watch-live log file path.

    ``~/.scitex/agent-container/runtime/logs/creds-watch.log`` — the
    runtime/logs dir is the documented home for daemon logs.
    """
    _home = home or Path.home()
    return (
        _home / ".scitex" / "agent-container" / "runtime" / "logs" / "creds-watch.log"
    )


def _emit(stream: TextIO, message: str) -> None:
    """Write one timestamped line to ``stream`` and flush."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stream.write(f"{ts} {message}\n")
    stream.flush()


def run_sync_once(
    log: TextIO,
    home: Path | None = None,
    store_dir: Path | None = None,
    *,
    now: float | None = None,
) -> None:
    """Run one :func:`sync_live` and log the outcome to ``log``.

    A :class:`LiveCredInvalidError` is logged (not raised) so the
    surrounding watch loop survives a transient invalid/expired live
    cred. Any other exception is logged and re-raised — a programming
    bug should not be silently swallowed.
    """
    try:
        result = sync_live(home=home, store_dir=store_dir, now=now)
    except LiveCredInvalidError as exc:
        _emit(log, f"live-cred-invalid: {exc}")
        return
    if result.action == "saved":
        _emit(
            log,
            f"saved {result.store_name} (email={result.email}, "
            f"expires_at={result.live_expires_at:.0f})",
        )
    else:
        _emit(log, f"up-to-date {result.store_name} (email={result.email})")


def _signature(path: Path) -> tuple[float, int] | None:
    """Return ``(mtime, size)`` of ``path``, or ``None`` if absent."""
    try:
        st = path.stat()
    except OSError:  # stx-allow: fallback (reason: file may not exist yet — None signals "no live cred", caller treats it as unchanged-absent)
        return None
    return (st.st_mtime, st.st_size)


def watch_poll(
    log: TextIO,
    home: Path | None = None,
    store_dir: Path | None = None,
    *,
    interval: float = DEFAULT_POLL_INTERVAL_S,
    iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Poll the live credential and sync on every change.

    Runs an initial sync, then watches the file signature
    (``mtime``+``size``) every ``interval`` seconds, syncing whenever it
    changes. ``iterations`` caps the number of poll cycles (``None`` =
    forever); the cap makes the loop smoke-testable against a tmp file.
    ``sleep_fn`` is injected so a test can drive the loop without real
    sleeps.
    """
    _home = home or Path.home()
    live = _home / ".claude" / ".credentials.json"

    _emit(log, f"watch-live poll started (interval={interval}s, path={live})")
    run_sync_once(log, home=home, store_dir=store_dir)
    last = _signature(live)

    count = 0
    while iterations is None or count < iterations:
        sleep_fn(interval)
        count += 1
        sig = _signature(live)
        if sig != last:
            _emit(log, "change detected")
            run_sync_once(log, home=home, store_dir=store_dir)
            last = sig


def _inotify_available() -> bool:
    """True when the ``inotifywait`` binary is on ``$PATH``."""
    return shutil.which("inotifywait") is not None


def watch_inotify(
    log: TextIO,
    home: Path | None = None,
    store_dir: Path | None = None,
) -> None:
    """Watch the live credential via ``inotifywait`` and sync on changes.

    Blocks on ``inotifywait -m`` and runs the engine on every
    close-write / move-in event against the credential file. Requires
    ``inotifywait`` on ``$PATH`` (caller checks via
    :func:`_inotify_available`). The credential parent dir is watched
    (not the file directly) so atomic-rename writes — claude-code's
    ``tmp`` + rename — still fire an event.
    """
    _home = home or Path.home()
    live = _home / ".claude" / ".credentials.json"
    watch_dir = live.parent

    _emit(log, f"watch-live inotify started (path={live})")
    run_sync_once(log, home=home, store_dir=store_dir)

    proc = subprocess.Popen(
        [
            "inotifywait",
            "-m",
            "-e",
            "close_write",
            "-e",
            "moved_to",
            "-e",
            "create",
            "--format",
            "%f",
            str(watch_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip() == live.name:
                _emit(log, "change detected")
                run_sync_once(log, home=home, store_dir=store_dir)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: best-effort shutdown of the watcher subprocess; escalate to kill so the daemon exit never hangs)
            proc.kill()


def run_watch(
    home: Path | None = None,
    store_dir: Path | None = None,
    *,
    log_path: Path | None = None,
    interval: float = DEFAULT_POLL_INTERVAL_S,
    prefer_inotify: bool = True,
) -> None:
    """Start the watch-live daemon, choosing inotify or poll automatically.

    Opens the log sink (``log_path`` file, or stderr when ``None``),
    then dispatches to :func:`watch_inotify` when ``inotifywait`` is
    available and ``prefer_inotify`` is set, else :func:`watch_poll`.
    Blocks until the process is signalled.
    """
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log: TextIO = log_path.open("a", encoding="utf-8")
        close_log = True
    else:
        log = sys.stderr
        close_log = False

    try:
        if prefer_inotify and _inotify_available():
            watch_inotify(log, home=home, store_dir=store_dir)
        else:
            if prefer_inotify:
                _emit(log, "inotifywait not found; falling back to poll")
            watch_poll(log, home=home, store_dir=store_dir, interval=interval)
    finally:
        if close_log:
            log.close()


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "default_log_path",
    "run_sync_once",
    "run_watch",
    "watch_inotify",
    "watch_poll",
]
