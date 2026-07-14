"""Stop sequence for the ``sac listen`` daemon — the half ``restart`` shares.

``sac listen stop`` and ``sac listen restart`` must never drift: a restart
IS a stop, followed by a relaunch and a health-probe. This module owns the
stop half as ONE implementation so the two verbs cannot diverge (SSOT).
``restart_listen`` used to run this sequence inline; it now calls
:func:`stop_listen`, and the behaviour is unchanged.

The sequence:

1. Read the PID from the flock-backed pidfile at
   ``<lock_dir>/listen-<port>.pid``.
2. SIGTERM it and poll for exit up to ``grace_secs``, escalating to
   SIGKILL on timeout (``force=True`` skips straight to SIGKILL).
3. **Verify the PID is really dead BEFORE clearing the pidfile** — never
   release a lock whose owner still lives, or a second daemon starts
   beside it.
4. Self-heal a WEDGED port holder the pidfile never named — the half-dead
   uvicorn that ``curl`` hangs on, whose pidfile may already be ``rm``-ed
   (incident 2026-06-26; see :mod:`._port_holder`).

**Idempotent**: stopping an already-stopped daemon is SUCCESS
(``ok=True``, ``was_running=False``) — the same contract as
``systemctl stop``. Only a daemon we could not kill is a failure.

**Seams.** Every primitive is reached through the ``_restart`` MODULE
OBJECT (``_restart._sleep``, ``_restart._terminate_then_kill``, …) and
resolved at CALL time — never bound via ``from ._restart import _sleep``
at import. Tests swap those module attributes to drive the sequence
without real signals; a from-import would capture the ORIGINAL callable
and silently defeat the swap, so the tests would pass while testing
nothing.

Pure-logic module — no click. Never raises: every failure lands in
:attr:`StopResult.error`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import _restart


@dataclass(frozen=True)
class StopResult:
    """Outcome of a :func:`stop_listen` invocation.

    ``ok`` is ``True`` for an idempotent no-op (nothing was running) —
    stopping a down daemon is success, not failure. :attr:`was_running`
    is what distinguishes the two for the CLI's report.

    ``port_holders_killed`` records any UNtracked PID(s) the self-heal
    force-killed off the port — empty when only the pidfile-tracked
    daemon was stopped.
    """

    ok: bool
    escalated_to_sigkill: bool
    had_prior_pidfile: bool
    prior_pid_alive: bool
    prior_pid: int | None = None
    port_holders_killed: tuple[int, ...] = ()
    error: str = ""

    @property
    def was_running(self) -> bool:
        """``True`` iff we actually stopped something.

        Either the pidfile named a live daemon, or an untracked remnant
        was still holding the port.
        """
        return self.prior_pid_alive or bool(self.port_holders_killed)


def stop_listen(
    *,
    host: str,
    port: int,
    lock_dir: Path,
    grace_secs: float = _restart.DEFAULT_GRACE_SECS,
    force: bool = False,
) -> StopResult:
    """Stop the ``sac listen`` daemon bound at ``host:port``.

    ``host``/``port`` mirror ``sac listen``'s own bind resolution;
    ``lock_dir`` matches ``_single_instance.default_lock_dir()``. Never
    raises — every failure populates :attr:`StopResult.error`.
    """
    pid_file = _restart.pidfile_path(port, lock_dir)
    prior_pid = _restart.read_pid_from_file(pid_file)
    had_prior_pidfile = pid_file.is_file()
    prior_alive = prior_pid is not None and _restart.pid_alive(prior_pid)

    escalated = False

    def _fail(error: str, *, killed: tuple[int, ...] = ()) -> StopResult:
        """Failure result capturing pre-state + self-heal progress so far."""
        return StopResult(
            ok=False,
            escalated_to_sigkill=escalated,
            had_prior_pidfile=had_prior_pidfile,
            prior_pid_alive=prior_alive,
            prior_pid=prior_pid,
            port_holders_killed=killed,
            error=error,
        )

    if prior_pid is not None and prior_alive:
        escalated = _restart._terminate_then_kill(
            prior_pid, grace_secs=grace_secs, force_kill=force
        )
        # Defence-in-depth: confirm the PID is actually dead before
        # touching the pidfile. If it survived SIGKILL, bail rather than
        # clear the lock and let a second daemon start beside it.
        _restart._sleep(_restart._POLL_INTERVAL_SECS)
        if _restart.pid_alive(prior_pid):
            return _fail(
                f"PID {prior_pid} survived SIGKILL — refusing to "
                f"clear pidfile or relaunch. Inspect manually "
                f"(zombie/uninterruptible state)."
            )

    try:
        pid_file.unlink(missing_ok=True)
    except OSError as exc:
        return _fail(f"failed to clear stale pidfile {pid_file}: {exc!r}")

    # Free the port from an UNtracked wedged remnant the pidfile never
    # named. The terminate/sleep seams are passed in so escalation stays
    # owned by ``_restart`` (and to avoid a circular import).
    heal = _restart.clear_wedged_port_holders(
        host=host,
        port=port,
        grace_secs=grace_secs,
        force=force,
        terminate_fn=_restart._terminate_then_kill,
        sleep_fn=_restart._sleep,
        poll_interval=_restart._POLL_INTERVAL_SECS,
    )
    if heal.error:
        return _fail(heal.error, killed=heal.killed)

    return StopResult(
        ok=True,
        escalated_to_sigkill=escalated,
        had_prior_pidfile=had_prior_pidfile,
        prior_pid_alive=prior_alive,
        prior_pid=prior_pid,
        port_holders_killed=heal.killed,
    )


__all__ = ["StopResult", "stop_listen"]
