"""Hot-standby + failover startup for ``sac listen`` (no more crash-loop).

Root cause this fixes (observed: ``sac-listen.service`` NRestarts
11→34+, ~4s CPU/cycle, wedged the systemd user manager): a SECOND
``sac listen`` launched while one already holds port 7878 used to raise
:class:`~._single_instance.ListenAlreadyRunningError`, which
``cli_pkg/listen_cmds.py`` turned into a ``click.ClickException`` →
**exit 1**. Under the unit's ``Restart=always`` that relaunched → failed
→ relaunched … an INFINITE CRASH-LOOP. The exit-1-on-contention was the
root cause.

The fix turns the second instance into a HOT STANDBY that FAILS OVER
instead of crash-looping. On startup :func:`resolve_startup` runs a
loop:

1. **Port free** (or the prior holder died — the kernel releases its
   flock on death) → :func:`~._single_instance.acquire_listen_lock`
   succeeds → return the handle → the caller binds + serves.
2. **Port held** (``acquire_listen_lock`` raises because a LIVE process
   holds the flock) → probe the holder's ``/v1/health`` (bounded):
   a. **Healthy** → do NOT exit; STAND BY: sleep a short interval, then
      re-check. When the holder later dies/wedges, take over → instant
      failover. This is the redundancy: the systemd instance stays a
      warm standby.
   b. **Unhealthy** (bounded GET fails/times out) for
      ``unhealthy_takeover_threshold`` consecutive cycles → the holder
      is wedged (alive, holds the flock+port, not serving) → TAKE OVER:
      kill the wedged holder (releases its flock) + clear any untracked
      port remnant, then loop → re-acquire.

**Race freedom.** The flock (``LOCK_EX | LOCK_NB``) is the ONLY gate
before binding and is atomic: at most one process can hold it, so two
instances can NEVER both bind. Take-over is therefore race-free —
every path funnels back through ``acquire_listen_lock``. If several
standbys wake on the same dead holder, each kills the (already-dead)
PID idempotently and re-acquires; the flock hands the lock to exactly
ONE winner, and every loser simply re-observes a healthy holder and
returns to standby.

**Startup-window safety.** A holder that just won the flock has a brief
window before it binds the port, during which a health probe would read
"not serving". The consecutive-cycle ``unhealthy_takeover_threshold``
(default 2) corroborates the wedged verdict across a standby interval
before killing, so a legitimately-starting daemon is never taken over.

**SIGTERM stays clean.** During standby the loop is interruptible:
:func:`standby_signal_guard` installs SIGTERM/SIGINT handlers that flip
a flag the loop polls (``_should_stop``), so ``systemctl stop`` during
standby returns a prompt clean exit — the wait never blocks the signal.
The guard restores the prior handlers on exit, so once the loop wins the
lock and returns, uvicorn installs its own graceful-shutdown handlers
unchanged.

No-mocks (PA-306 / STX-NM001-003): every external effect is a
module-level seam swapped via save/restore in tests; the health probe
runs against a real loopback socket and the lock against a real flock.
"""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from ._port_holder import clear_wedged_port_holders
from ._restart import (
    HEALTH_PATH,
    pid_alive,
    pidfile_path,
    read_pid_from_file,
)
from ._restart import _default_http_get as _http_get_impl
from ._restart import _terminate_then_kill as _terminate_impl
from ._single_instance import (
    ListenAlreadyRunningError,
    LockHandle,
    acquire_listen_lock,
    release_listen_lock,
)

__all__ = [
    "DEFAULT_HEALTH_TIMEOUT_SECS",
    "DEFAULT_STANDBY_INTERVAL_SECS",
    "DEFAULT_TAKEOVER_GRACE_SECS",
    "DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD",
    "resolve_startup",
    "standby_signal_guard",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Standby re-check cadence. ~4s (operator brief: ~3-5s) keeps failover
# latency low without a hot poll loop.
DEFAULT_STANDBY_INTERVAL_SECS: float = 4.0
# SIGTERM window granted to a wedged holder before SIGKILL during a
# take-over. Short — the holder is already not serving.
DEFAULT_TAKEOVER_GRACE_SECS: float = 5.0
# Per-probe health timeout. BOUNDED so a hung/zombie holder is detected
# unhealthy → failover, never an infinite wait.
DEFAULT_HEALTH_TIMEOUT_SECS: float = 2.0
# Consecutive unhealthy cycles required before a take-over kills the
# holder. Corroborates the verdict across a standby interval so a
# daemon that merely hasn't finished binding yet is not killed.
DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD: int = 2

# Sub-slice the standby sleep so a SIGTERM flag is noticed within
# ~250ms even though the sleep seam itself is not signal-aware.
_STANDBY_POLL_SLICE_SECS: float = 0.25
# Poll granularity handed to the port-holder self-heal.
_POLL_INTERVAL_SECS: float = 0.2


# ---------------------------------------------------------------------------
# Module-level seams (swapped via save/restore in tests, NO MagicMock).
# ---------------------------------------------------------------------------


def _default_probe_health(host: str, port: int, *, timeout: float) -> bool:
    """True iff the holder answered ``/v1/health`` with ANY HTTP status.

    Mirrors ``_restart.wait_for_health``'s liveness contract: a 200 —
    but ALSO a 401/403 under bearer auth — proves the daemon is up and
    serving; only a transport failure (refused / timeout) is "down".
    Bounded by ``timeout`` so a wedged holder cannot hang the probe.
    """
    url = f"http://{host}:{port}{HEALTH_PATH}"
    return _http_get_impl(url, timeout) > 0


def _default_take_over(
    *,
    host: str,
    port: int,
    lock_dir: Path,
    holder_pid: int | None,
    grace_secs: float,
) -> str:
    """Free the port from a wedged holder so a re-acquire can serve.

    Kills the tracked flock holder named by the pidfile (releasing its
    flock even if it never bound the port), THEN clears any untracked
    remnant still holding the port, THEN drops the now-stale pidfile.
    Returns ``""`` on success or a LOUD error string when the port
    could not be freed (an unkillable holder). Never raises.
    """
    if holder_pid is not None and pid_alive(holder_pid):
        _terminate(holder_pid, grace_secs=grace_secs, force_kill=False)

    heal = _clear_port(
        host=host,
        port=port,
        grace_secs=grace_secs,
        force=False,
        terminate_fn=_terminate,
        sleep_fn=_sleep,
        poll_interval=_POLL_INTERVAL_SECS,
    )

    try:
        pidfile_path(port, lock_dir).unlink()
    except FileNotFoundError:  # stx-allow: fallback (reason: already gone — the goal state)
        pass
    except OSError:  # stx-allow: fallback (reason: best-effort — a leftover pidfile is a stale diagnostic the next acquire overwrites)
        pass
    return heal.error


def _default_heal_untracked_port(*, host: str, port: int, grace_secs: float) -> str:
    """Clear an UNtracked remnant still on the port after we won the flock.

    We hold the flock, so no TRACKED ``sac listen`` holds the port; a
    bound port here is a wedged remnant that would EADDRINUSE our bind.
    No-op when the port is free. Returns ``""`` or a LOUD error when the
    remnant is unkillable.
    """
    return _clear_port(
        host=host,
        port=port,
        grace_secs=grace_secs,
        force=False,
        terminate_fn=_terminate,
        sleep_fn=_sleep,
        poll_interval=_POLL_INTERVAL_SECS,
    ).error


def _default_log(message: str) -> None:
    """Emit a transition line (→ journal / runtime/listen.log).

    Routed through scitex-logging rather than a raw stderr print so a failover
    transition carries its origin and lands in the runtime log as well as the
    journal. The module-level ``_log`` indirection below is untouched — tests
    swap that callable wholesale, and that seam is the reason this function is
    separate in the first place.
    """
    from .._logging import get_logger

    get_logger(__name__).info(message)


_acquire: Callable[..., LockHandle] = acquire_listen_lock
_release: Callable[[LockHandle], None] = release_listen_lock
_read_pid: Callable[[Path], "int | None"] = read_pid_from_file
_terminate: Callable[..., bool] = _terminate_impl
_clear_port = clear_wedged_port_holders
_probe_health: Callable[..., bool] = _default_probe_health
_take_over: Callable[..., str] = _default_take_over
_heal_untracked_port: Callable[..., str] = _default_heal_untracked_port
_sleep: Callable[[float], None] = time.sleep
_should_stop: Callable[[], bool] = lambda: False  # noqa: E731 (seam; swapped by the signal guard / tests)
_log: Callable[[str], None] = _default_log


# ---------------------------------------------------------------------------
# Interruptible standby wait
# ---------------------------------------------------------------------------


def _sleep_unless_stopped(interval: float) -> None:
    """Sleep ``interval`` seconds but bail the instant a stop is flagged.

    Polls ``_should_stop`` on ``_STANDBY_POLL_SLICE_SECS`` boundaries so
    a SIGTERM during standby is honoured within ~250ms — the wait never
    blocks the signal.
    """
    if interval <= 0:
        return
    step = min(_STANDBY_POLL_SLICE_SECS, interval)
    slept = 0.0
    while slept < interval:
        if _should_stop():
            return
        _sleep(step)
        slept += step


def _pid_str(pid: "int | None") -> str:
    return str(pid) if pid is not None else "<unknown>"


# ---------------------------------------------------------------------------
# Top-level: resolve_startup
# ---------------------------------------------------------------------------


def resolve_startup(
    *,
    host: str,
    port: int,
    lock_dir: Path,
    standby_interval: float = DEFAULT_STANDBY_INTERVAL_SECS,
    takeover_grace_secs: float = DEFAULT_TAKEOVER_GRACE_SECS,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT_SECS,
    unhealthy_takeover_threshold: int = DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD,
) -> LockHandle | None:
    """Acquire the listen lock, standing by / failing over as needed.

    Returns a held :class:`LockHandle` the caller must keep for the
    lifetime of the daemon (release on exit), or ``None`` when a stop
    signal arrived while standing by (the caller should exit cleanly
    WITHOUT binding). Never raises on contention — that is the whole
    point; a duplicate launch stands by instead of crash-looping.

    The flock remains the atomic bind arbiter, so this never lets two
    instances bind at once regardless of how many stand by.
    """
    pidfile = pidfile_path(port, lock_dir)
    threshold = unhealthy_takeover_threshold if unhealthy_takeover_threshold > 0 else 1
    consecutive_unhealthy = 0

    while True:
        if _should_stop():
            _log(
                "# sac listen: shutdown signal received while standing by "
                "— exiting without binding"
            )
            return None

        try:
            handle = _acquire(port=port, lock_dir=lock_dir)
        except ListenAlreadyRunningError:
            # A LIVE process holds the flock (the kernel releases the
            # flock on death, so a held flock == a live holder).
            holder_pid = _read_pid(pidfile)

            if _probe_health(host, port, timeout=health_timeout):
                consecutive_unhealthy = 0
                _log(
                    f"# sac listen: standing by behind healthy holder "
                    f"PID {_pid_str(holder_pid)} on {host}:{port}"
                )
                _sleep_unless_stopped(standby_interval)
                continue

            consecutive_unhealthy += 1
            if consecutive_unhealthy < threshold:
                # Corroborate across a standby interval before killing —
                # a holder that just won the flock may not have bound
                # the port yet. Do not take over on a single miss.
                _log(
                    f"# sac listen: holder PID {_pid_str(holder_pid)} on "
                    f"{host}:{port} not answering health "
                    f"({consecutive_unhealthy}/{threshold}) — re-checking "
                    f"in {standby_interval}s before taking over"
                )
                _sleep_unless_stopped(standby_interval)
                continue

            _log(
                f"# sac listen: holder PID {_pid_str(holder_pid)} on "
                f"{host}:{port} is wedged (not serving) — taking over"
            )
            error = _take_over(
                host=host,
                port=port,
                lock_dir=lock_dir,
                holder_pid=holder_pid,
                grace_secs=takeover_grace_secs,
            )
            consecutive_unhealthy = 0
            if error:
                _log(
                    f"# sac listen: take-over incomplete — {error} — "
                    f"standing by, will retry"
                )
                _sleep_unless_stopped(standby_interval)
            # Loop: re-acquire. The flock arbitrates any standby race.
            continue

        # We hold the flock: the sole authorized binder. An UNtracked
        # remnant may still hold the PORT (flock was free, port wasn't)
        # → self-heal it so uvicorn does not hit EADDRINUSE.
        heal_error = _heal_untracked_port(
            host=host, port=port, grace_secs=takeover_grace_secs
        )
        if heal_error:
            # Cannot free the port. Releasing the flock + standing by is
            # strictly better than crash-looping into EADDRINUSE.
            _log(
                f"# sac listen: port {port} held by an unkillable remnant "
                f"— {heal_error} — releasing lock, standing by"
            )
            _release(handle)
            _sleep_unless_stopped(standby_interval)
            continue

        _log(f"# sac listen: acquired {host}:{port} — serving")
        return handle


# ---------------------------------------------------------------------------
# Signal guard — clean, prompt exit on SIGTERM/SIGINT during standby
# ---------------------------------------------------------------------------


class _StopFlag:
    """A one-way stop flag flipped by a signal handler, polled by the loop."""

    __slots__ = ("_tripped",)

    def __init__(self) -> None:
        self._tripped = False

    def trip(self, *_args: object) -> None:
        """Signal-handler entrypoint — minimal work: flip the flag."""
        self._tripped = True

    def is_set(self) -> bool:
        return self._tripped


@contextmanager
def standby_signal_guard() -> Iterator[None]:
    """Make the standby loop exit cleanly + promptly on SIGTERM/SIGINT.

    Installs handlers that flip a flag the loop polls (via the
    ``_should_stop`` seam) instead of letting SIGTERM's default
    disposition abruptly terminate the process or SIGINT raise
    ``KeyboardInterrupt`` mid-standby. Restores the prior handlers on
    exit so uvicorn (started only once the loop owns the lock) installs
    its own graceful-shutdown handlers unchanged.

    Handlers can only be installed from the main thread; the ``sac
    listen`` CLI runs there. Off the main thread this degrades to a
    no-op guard (keeps the prior never-stop behaviour) rather than
    crashing the boot.
    """
    global _should_stop
    flag = _StopFlag()
    try:
        prev_term = signal.signal(signal.SIGTERM, flag.trip)
        prev_int = signal.signal(signal.SIGINT, flag.trip)
    except ValueError:
        # Not the main thread — cannot install signal handlers.
        yield
        return

    saved_should_stop = _should_stop
    _should_stop = flag.is_set
    try:
        yield
    finally:
        _should_stop = saved_should_stop
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
