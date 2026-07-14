"""Startup decision for ``sac listen``: bind, stand down, or fail loud.

``sac listen`` starting while another instance already holds port 7878 has
exactly THREE possible answers, and this module picks one in BOUNDED time:

1. **Nobody is holding it** (or the holder died — the kernel releases its
   flock on death) → take the flock, heal any untracked port remnant,
   BIND and serve.
2. **Another instance is ALREADY SERVING** (it answers ``/v1/health``) →
   the goal state is already true. Say so and **EXIT 0. Nothing to do.**
3. **The holder is NOT serving** and cannot be freed → **FAIL LOUD**
   (non-zero, naming the PID and the exact command to run).

There is no fourth answer. In particular there is **no "spin" answer.**

Why (operator incidents, 2026-07-14)
====================================
This module used to run an UNBOUNDED standby loop, and it hurt twice.

**(a) A healthy holder made ``sac listen`` hang forever.** ::

    $ sac listen
    # sac listen: standing by behind healthy holder PID 745734 on 127.0.0.1:7878
    # sac listen: standing by behind healthy holder PID 745734 on 127.0.0.1:7878
       ... (forever) ...
    ^C
    $ kill 745734          <-- the operator had to do this BY HAND

"Someone else is already serving" is a SUCCESS, not a reason to poll every
4 seconds until the human gives up. The spinner was the only way out, so he
Ctrl-C'd and killed the holder manually — and every abandoned spinner left
junk behind (two orphaned ``sac-listen`` screen sessions were found this
way). Waiting bought nothing: a standby holds no lock and serves no request.
And taking over a HEALTHY holder is not merely useless but harmful — a
listen restart tears down the in-process a2a Broker and deafens EVERY
agent's inbox at once. So: verified serving ⇒ stand down, exit 0.

**(b) A DEAD holder was announced as "healthy".** ::

    # sac listen: holder PID 738982 ... not answering health (1/2) — re-checking
    # sac listen: standing by behind healthy holder PID 738982 on 127.0.0.1:7878
    # sac listen: standing by behind healthy holder PID 738982 on 127.0.0.1:7878

…while PID 738982 answered nothing and the fleet was cut off from the host.
It SAW the failed check, then went back to calling the holder healthy. Two
causes, both fixed:

* ``consecutive_unhealthy = 0`` was wiped by ANY single lucky reply, so a
  FLAPPING holder (the measured behaviour of the live 7878 daemon: HTTP 200
  one minute, ``Connection refused`` the next) never accumulated the
  CONSECUTIVE failures the threshold demanded. Now
  :class:`~._standby_ledger.HolderLedger` keeps the record: a failure is a
  fact, and one later success does not un-happen it.
* The probe was ``_http_get(…) > 0``, so any HTTP status — even a 5xx —
  read as health. Now the verdict is the three-state
  :class:`~._holder_health.HolderHealth`; "I asked and got nothing" is a
  state the code can express, and it is NOT health.

Never destroy on a negative signal
==================================
A take-over ASKS (SIGTERM, via :func:`._graceful_stop.terminate_gracefully`)
and never escalates. A probe-based "wedged" verdict can be wrong, and the
remedy for a wrong one is to SIGKILL a healthy control plane — a false-RED
is strictly worse than a false-green. A holder that ignores SIGTERM is
handed to the operator with the one destructive command they can authorise
(``sac listen restart --force``). The take-over also never unlinks a LIVE
holder's pidfile: a racing standby holding an flock on the old inode plus a
fresh acquirer creating a new one would BOTH think they held the lock.

The flock (``LOCK_EX | LOCK_NB``) remains the atomic bind arbiter, so two
instances can never both bind, no matter how many start at once.

No-mocks (PA-306 / STX-NM001-003): every external effect is a module-level
seam swapped via save/restore in tests; the health probe runs against a real
loopback socket and the lock against a real flock.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from ._graceful_stop import terminate_gracefully as _terminate_graceful_impl
from ._holder_health import HEALTH_PATH, HolderProbe
from ._holder_health import probe_holder_health as _probe_health_impl
from ._port_holder import clear_wedged_port_holders
from ._restart import (
    pid_alive,
    pidfile_path,
    read_pid_from_file,
)
from ._restart import _terminate_then_kill as _terminate_impl
from ._single_instance import (
    ListenAlreadyRunningError,
    LockHandle,
    acquire_listen_lock,
    release_listen_lock,
)
from ._standby_ledger import (
    HolderLedger,
    ListenTakeoverFailed,
    takeover_failure_message,
)
from ._standby_signal import stop_flag_guard

__all__ = [
    "DEFAULT_HEALTH_TIMEOUT_SECS",
    "DEFAULT_MAX_TAKEOVER_ATTEMPTS",
    "DEFAULT_RECHECK_INTERVAL_SECS",
    "DEFAULT_TAKEOVER_GRACE_SECS",
    "DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD",
    "HEALTH_PATH",
    "ListenTakeoverFailed",
    "resolve_startup",
    "standby_signal_guard",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Pause between the re-checks that CORROBORATE a failed health check. This
# is NOT a standby cadence — a serving holder is never re-checked, we exit
# at once. It only spaces out the (bounded) probes of a SUSPECT holder.
DEFAULT_RECHECK_INTERVAL_SECS: float = 4.0
# Back-compat alias: the old name for the same knob.
DEFAULT_STANDBY_INTERVAL_SECS: float = DEFAULT_RECHECK_INTERVAL_SECS
# SIGTERM window granted to a wedged holder during a take-over. We never
# escalate past it — see the module docstring.
DEFAULT_TAKEOVER_GRACE_SECS: float = 5.0
# Per-probe health timeout. BOUNDED so a hung holder is detected, never an
# infinite wait.
DEFAULT_HEALTH_TIMEOUT_SECS: float = 2.0
# Failed health checks required before the wedged verdict is corroborated
# and acted on. Corroboration protects a daemon that merely has not
# finished binding yet from being taken over on one unlucky probe.
DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD: int = 2
# How many times we may ASK a non-serving holder to leave before we stop
# and FAIL LOUD. A holder that will not exit is a human's problem.
DEFAULT_MAX_TAKEOVER_ATTEMPTS: int = 3
# Hard ceiling on decision iterations. Belt-and-braces: NO input may make
# this loop run forever, because an unbounded loop is the bug.
_MAX_DECISION_CYCLES: int = 32

# Sub-slice the re-check pause so a SIGTERM is noticed within ~250ms.
_POLL_SLICE_SECS: float = 0.25
# Poll granularity handed to the port-holder self-heal.
_POLL_INTERVAL_SECS: float = 0.2


# ---------------------------------------------------------------------------
# Module-level seams (swapped via save/restore in tests, NO MagicMock).
# ---------------------------------------------------------------------------


def _default_probe_health(host: str, port: int, *, timeout: float) -> HolderProbe:
    """Ask the holder ``/v1/health`` and report WHAT IT ACTUALLY DID.

    Returns a three-state :class:`~._holder_health.HolderProbe`, never a
    bool: "I asked and got nothing" must be expressible and must not be
    silently equal to "it said no". Bounded by ``timeout``.
    """
    return _probe_health_impl(host, port, timeout=timeout)


def _default_take_over(
    *,
    host: str,
    port: int,
    lock_dir: Path,
    holder_pid: int | None,
    grace_secs: float,
) -> str:
    """ASK the wedged holder to exit so a re-acquire can serve. Never SIGKILL.

    Returns ``""`` when the holder is gone (the kernel releases its flock,
    so the next ``acquire_listen_lock`` wins the port), or a LOUD error
    string when it refused to leave. Never raises.

    Deliberately does NOT unlink the pidfile: the flock — not the file's
    existence — is the bind arbiter, and the next acquirer truncates and
    rewrites the body anyway. Unlinking a LIVE holder's pidfile would let a
    racing standby (flocking the OLD inode) and a fresh acquirer (creating
    a NEW inode) BOTH believe they hold the lock, and both bind.
    """
    if holder_pid is None:
        # Nothing tracked to stop. Any remnant on the PORT is handled by
        # the serve path's heal once we hold the flock.
        return ""
    if not pid_alive(holder_pid):
        return ""  # already gone — the kernel released its flock

    if _terminate_graceful(holder_pid, grace_secs=grace_secs):
        return ""

    return (
        f"holder PID {holder_pid} ignored SIGTERM after {grace_secs}s and "
        f"still holds the {host}:{port} lock. Refusing to SIGKILL it "
        f"automatically — a probe-based 'wedged' verdict can be wrong, and "
        f"force-killing a healthy control plane would cut the whole fleet "
        f"off from this host. Run `sac listen restart --force` to force it."
    )


def _default_heal_untracked_port(*, host: str, port: int, grace_secs: float) -> str:
    """Clear an UNtracked remnant still on the port after we won the flock.

    We hold the flock, so no TRACKED ``sac listen`` holds the port; a bound
    port here is a wedged remnant that would EADDRINUSE our bind. No-op
    when the port is free. Returns ``""`` or a LOUD error when unkillable.
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
    """Emit a transition line to stderr (→ journal / runtime/listen.log)."""
    print(message, file=sys.stderr, flush=True)


_acquire: Callable[..., LockHandle] = acquire_listen_lock
_release: Callable[[LockHandle], None] = release_listen_lock
_read_pid: Callable[[Path], "int | None"] = read_pid_from_file
_terminate: Callable[..., bool] = _terminate_impl
_terminate_graceful: Callable[..., bool] = _terminate_graceful_impl
_clear_port = clear_wedged_port_holders
_probe_health: Callable[..., HolderProbe] = _default_probe_health
_take_over: Callable[..., str] = _default_take_over
_heal_untracked_port: Callable[..., str] = _default_heal_untracked_port
_sleep: Callable[[float], None] = time.sleep
_should_stop: Callable[[], bool] = lambda: False  # noqa: E731 (seam; swapped by the signal guard / tests)
_log: Callable[[str], None] = _default_log


def _sleep_unless_stopped(interval: float) -> None:
    """Sleep ``interval`` seconds but bail the instant a stop is flagged.

    Polls ``_should_stop`` on ``_POLL_SLICE_SECS`` boundaries so a SIGTERM
    during a re-check pause is honoured within ~250ms.
    """
    if interval <= 0:
        return
    step = min(_POLL_SLICE_SECS, interval)
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
    standby_interval: float = DEFAULT_RECHECK_INTERVAL_SECS,
    takeover_grace_secs: float = DEFAULT_TAKEOVER_GRACE_SECS,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT_SECS,
    unhealthy_takeover_threshold: int = DEFAULT_UNHEALTHY_TAKEOVER_THRESHOLD,
    max_takeover_attempts: int = DEFAULT_MAX_TAKEOVER_ATTEMPTS,
) -> LockHandle | None:
    """Decide, in BOUNDED time, what this ``sac listen`` should do.

    Returns:
        * a held :class:`LockHandle` — we own the port; the caller BINDS.
        * ``None`` — do NOT bind; exit 0. Either another instance is
          already serving (nothing to do) or a stop signal arrived. The
          reason has already been logged.

    Raises:
        ListenTakeoverFailed: the holder is NOT serving and the port could
            not be freed. The CLI maps this to a non-zero exit carrying the
            PID and the exact remedy.

    There is deliberately NO "spin" outcome: this function always reaches a
    decision, and it never polls a healthy holder.
    """
    pidfile = pidfile_path(port, lock_dir)
    ledger = HolderLedger(threshold=unhealthy_takeover_threshold)
    takeover_attempts = 0
    heal_attempts = 0
    max_attempts = max(1, max_takeover_attempts)

    for _cycle in range(_MAX_DECISION_CYCLES):
        if _should_stop():
            _log("# sac listen: shutdown signal received — exiting without binding")
            return None

        try:
            handle = _acquire(port=port, lock_dir=lock_dir)
        except ListenAlreadyRunningError:
            # A LIVE process holds the flock (the kernel releases it on
            # death, so a held flock == a live holder). But a live PID is
            # NOT health — ask the PORT what it is actually doing.
            holder_pid = _read_pid(pidfile)
            probe = _probe_health(host, port, timeout=health_timeout)
            ledger.record(probe)

            if not ledger.suspect:
                # ANOTHER INSTANCE IS ALREADY SERVING → the goal state is
                # already true. EXIT 0. Do NOT stand by, and do NOT take
                # over: a listen restart tears down the in-process a2a
                # Broker and deafens every agent's inbox at once, so
                # displacing a working holder is pure damage.
                _log(
                    f"# sac listen: already serving: PID {_pid_str(holder_pid)} "
                    f"on {host}:{port} ({probe.describe()}) — nothing to do"
                )
                return None

            if not ledger.corroborated:
                # Corroborate across a short pause before acting — a holder
                # that just won the flock may not have bound the port yet.
                #
                # Unlike the counter this replaces, a failure is NOT erased
                # by the next lucky reply: THAT reset is what let a flapping
                # holder be announced "healthy" forever. So say plainly what
                # is on the books, whichever way this particular probe went.
                if probe.serving:
                    _log(
                        f"# sac listen: holder PID {_pid_str(holder_pid)} on "
                        f"{host}:{port} answered this time ({probe.describe()}) "
                        f"but still carries {ledger.failures}/{ledger.threshold} "
                        f"failed health check(s) — NOT standing down on one "
                        f"lucky reply; re-checking in {standby_interval}s"
                    )
                else:
                    _log(
                        f"# sac listen: holder PID {_pid_str(holder_pid)} on "
                        f"{host}:{port} FAILED its health check "
                        f"({ledger.failures}/{ledger.threshold}) — "
                        f"{probe.describe()} — re-checking in {standby_interval}s"
                    )
                _sleep_unless_stopped(standby_interval)
                continue

            takeover_attempts += 1
            _log(
                f"# sac listen: holder PID {_pid_str(holder_pid)} on "
                f"{host}:{port} is NOT SERVING — {probe.describe()} — "
                f"corroborated over {ledger.failures} failed checks. Asking "
                f"it to exit (SIGTERM, attempt {takeover_attempts}/"
                f"{max_attempts}); it will NOT be SIGKILLed automatically."
            )
            error = _take_over(
                host=host,
                port=port,
                lock_dir=lock_dir,
                holder_pid=holder_pid,
                grace_secs=takeover_grace_secs,
            )
            if not error:
                # It left (or was already gone). Re-acquire; the flock
                # arbitrates any race between several starters.
                ledger.reset()
                continue

            if takeover_attempts >= max_attempts:
                raise ListenTakeoverFailed(
                    takeover_failure_message(
                        host=host,
                        port=port,
                        holder_pid=holder_pid,
                        probe=probe,
                        failures=ledger.failures,
                        attempts=takeover_attempts,
                        error=error,
                    )
                )
            _log(
                f"# sac listen: take-over attempt {takeover_attempts}/"
                f"{max_attempts} did not free {host}:{port} — {error}"
            )
            _sleep_unless_stopped(standby_interval)
            continue

        # We hold the flock: the sole authorized binder. An UNtracked
        # remnant may still hold the PORT (flock was free, port wasn't)
        # → self-heal it so uvicorn does not hit EADDRINUSE.
        heal_error = _heal_untracked_port(
            host=host, port=port, grace_secs=takeover_grace_secs
        )
        if heal_error:
            heal_attempts += 1
            _release(handle)
            if heal_attempts >= max_attempts:
                raise ListenTakeoverFailed(
                    f"ERROR: sac listen cannot bind {host}:{port} — the port "
                    f"is held by an unkillable remnant after {heal_attempts} "
                    f"attempts: {heal_error}\n"
                    f"  Refusing to spin silently behind it. To force the "
                    f"take-over, run:  sac listen restart --force"
                )
            _log(
                f"# sac listen: port {port} held by an unkillable remnant "
                f"({heal_attempts}/{max_attempts}) — {heal_error} — "
                f"released lock, retrying"
            )
            _sleep_unless_stopped(standby_interval)
            continue

        _log(f"# sac listen: acquired {host}:{port} — serving")
        return handle

    # Belt-and-braces: no input may make this loop run forever.
    raise ListenTakeoverFailed(
        f"ERROR: sac listen could not reach a decision about {host}:{port} "
        f"after {_MAX_DECISION_CYCLES} cycles (the flock keeps changing "
        f"hands). Refusing to spin. Inspect with `sac listen status`, then "
        f"`sac listen restart --force`."
    )


# ---------------------------------------------------------------------------
# Signal guard — clean, prompt exit on SIGTERM/SIGINT mid-decision
# ---------------------------------------------------------------------------


@contextmanager
def standby_signal_guard() -> Iterator[None]:
    """Make the startup decision exit cleanly + promptly on SIGTERM/SIGINT.

    Wires :func:`._standby_signal.stop_flag_guard`'s flag into the
    ``_should_stop`` seam the decision loop polls, and restores the prior
    ``_should_stop`` (and the prior OS handlers) on exit — so uvicorn,
    started only once the loop owns the lock, installs its own
    graceful-shutdown handlers unchanged.
    """
    global _should_stop
    with stop_flag_guard() as flag:
        saved_should_stop = _should_stop
        _should_stop = flag.is_set
        try:
            yield
        finally:
            _should_stop = saved_should_stop
