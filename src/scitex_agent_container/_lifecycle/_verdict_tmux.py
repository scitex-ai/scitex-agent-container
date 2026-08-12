"""Can we SEE the host's tmux and the host's pid namespace from where we are?

Split out of :mod:`._verdict_resolve` (512-line cap). Every function here answers
one question: **is this instrument a sensor from HERE, or are we blind?** — and
blindness must always come back as "I could not look", never as "nothing is
there".

That distinction is not academic. Both of the sensors this module fronts are
namespace-scoped, and a container sits in a DIFFERENT namespace:

* **tmux** — from inside a SIF, ``tmux ls`` prints *"no server running on
  /tmp/tmux-1000/default"*. True! of the CONTAINER's own ``/tmp``. That is one of
  ``_tmux_probe``'s "no server ⇒ confirmed-empty" markers, so the probe does not
  fail — it SUCCEEDS and reports an EMPTY fleet.
* **pids** — from inside a SIF, ``os.kill(host_pid, 0)`` raises ESRCH because
  that pid does not exist in OUR pid namespace. Worse, the number may be reused
  by an unrelated container process, in which case it lies in the other
  direction.

MEASURED 2026-07-14: run from inside this container, the tmux path made
``process_signal`` return DEAD for ``grant`` — an agent holding a live tmux
session, a fresh heartbeat and a live inbox subscriber on the host. A confident,
well-evidenced, completely false death verdict.

A pid or a session read across a namespace boundary is not a WEAK sensor. It is
NOT A SENSOR. It must return UNKNOWN, in both directions.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "in_sif",
    "observed_session_snapshot",
    "pid_namespace_is_observable",
    "session_name_for_config",
    "tmux_probe_ran",
    "tmux_session_observation",
]


def in_sif() -> bool:
    """Are we running INSIDE an apptainer SIF (so the host's tmux/pids are gone)?

    Reuses the canonical predicate the spawn/status brokers already key off
    (:func:`.._lifecycle._in_sif_broker.is_in_sif`) rather than sniffing for
    apptainer markers a second time — one definition of "am I in a container",
    so the probe and the brokers can never disagree.

    Fails CAUTIOUS: if we cannot even tell where we are, assume we might be
    blind. That direction costs us a refused destruction; the other direction
    costs us a destroyed agent.
    """
    try:
        from ._in_sif_broker import is_in_sif

        return bool(is_in_sif())
    except Exception:  # stx-allow: fallback (if we cannot even tell where we are, assume the cautious answer: we might be blind)
        return True


def pid_namespace_is_observable(
    *,
    row_host: str | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
    local_host_fn: Callable[[], str] | None = None,
) -> tuple[bool, str]:
    """Is ``os.kill(pid, 0)`` a SENSOR from here? ``(observable, why_not)``.

    Two ways a recorded pid is not ours to read, and both must degrade to
    UNKNOWN rather than to a confident DEAD:

    1. **We are in a container.** A host pid is not in our pid namespace, so
       ``os.kill`` answers about a different process — or none.
    2. **The row belongs to another host.** Its pid was minted on a machine we
       cannot see; the same integer here is an unrelated local process.

    ``row_host=None`` skips check 2 (the caller has no host to compare).
    """
    if (in_sif_fn or in_sif)():
        return False, (
            "we are INSIDE a container, so the recorded pid lives in the HOST's "
            "pid namespace and os.kill here cannot see it — a pid read across a "
            "namespace boundary is not a weak sensor, it is NOT A SENSOR"
        )
    if row_host:
        local = (local_host_fn or _local_host)()
        if local and row_host != local:
            return False, (
                f"the row was written on host {row_host!r} but we are on "
                f"{local!r} — that pid was minted in another machine's namespace, "
                f"so os.kill here would be asking about an unrelated local process"
            )
    return True, ""


def _local_host() -> str:
    """This machine's name, as the ``instances`` table records it."""
    import socket

    try:
        return socket.gethostname()
    except Exception:  # stx-allow: fallback (an unknowable hostname must not manufacture a cross-host mismatch — return "" so the caller skips the check)
        return ""


def _real_tmux_snapshot(*, socket_name: str | None = None) -> dict | None:
    """The real batched tmux probe, behind a seam so tests drive the RULE."""
    from .._runners._tmux._tmux_probe import list_sessions_activity

    return list_sessions_activity(socket_name=socket_name)


def _observed_snapshot(
    socket_name: str | None,
    snapshot_fn: Callable[..., dict | None] | None,
    in_sif_fn: Callable[[], bool] | None,
) -> dict | None:
    """The tmux snapshot IF we were actually in a position to take one.

    ``None`` means "I could not look" — which covers BOTH ways the probe fails
    to see the fleet, and both must map here:

    1. the probe errored / tmux is wedged (``list_sessions_activity`` already
       returns ``None`` for this, exactly as its contract says);
    2. we are inside a container and the host's tmux is in another mount
       namespace — the TRAP, because the probe does not error, it SUCCEEDS and
       reports an EMPTY fleet.
    """
    snapshot_fn = snapshot_fn or _real_tmux_snapshot
    in_sif_fn = in_sif_fn or in_sif

    try:
        snapshot = snapshot_fn(socket_name=socket_name)
    except Exception:  # stx-allow: fallback (cannot even ask tmux → we do not know whether a probe would have run)
        return None

    if snapshot is None:
        return None  # the probe FAILED — its own contract already says so.
    if not snapshot and in_sif_fn():
        # An "empty fleet" seen from inside a container is namespace blindness,
        # not an empty fleet. Refuse to treat a non-observation as one.
        return None
    return snapshot


def observed_session_snapshot(
    socket_name: str | None = None,
    *,
    snapshot_fn: Callable[..., dict | None] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> dict | None:
    """ONE batched tmux reading, for a caller that must judge MANY agents.

    Same contract as :func:`_observed_snapshot` — a ``dict`` of live sessions
    when we were genuinely in a position to look, ``None`` when we were not
    (wedged tmux, or the container-blindness trap where an empty result is a
    namespace boundary rather than an empty fleet).

    Public because a route that annotates every registry row must take the
    reading ONCE. :func:`tmux_session_observation` re-probes per call, so
    asking it about N agents costs N ``tmux`` subprocess spawns — the exact
    O(N)-spawns shape that blew the heartbeat tick's budget and got it
    abandoned (see :func:`.._runners._tmux._tmux_probe.list_sessions_activity`).
    Callers look a name up in the returned dict instead.

    A thin wrapper rather than a second implementation: the blindness rule
    lives in exactly one place, so a batched caller and a per-agent caller can
    never drift into disagreeing about what "I could not look" means.
    """
    return _observed_snapshot(socket_name, snapshot_fn, in_sif_fn)


def tmux_probe_ran(
    socket_name: str | None = None,
    *,
    snapshot_fn: Callable[..., dict | None] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> bool | None:
    """Did a tmux probe run that could actually SEE this fleet's sessions?

    ``True`` = yes, so a "no session" answer is a real observation of absence.
    ``None`` = no, so a "no session" answer means "I could not look".
    """
    snapshot = _observed_snapshot(socket_name, snapshot_fn, in_sif_fn)
    return True if snapshot is not None else None


def tmux_session_observation(
    session_name: str,
    *,
    socket_name: str | None = None,
    snapshot_fn: Callable[..., dict | None] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> tuple[bool | None, bool | None]:
    """``(probe_ran, session_present)`` — and WHICH instrument may speak.

    This exists because ``TuiSessionRuntime.is_running`` is a CONJUNCTION —
    ``tmux session exists`` AND ``os.kill(pane_pid, 0)`` — collapsed into one
    bool. Its ``False`` therefore has two very different authors, and they are
    two DIFFERENT INSTRUMENTS:

    * the tmux server says there is no such session  → ``host_tmux`` observed it;
    * the session EXISTS but its pane pid is reaped  → ``pid_namespace`` observed
      it, which is THE SAME ``os.kill(pane_pid, 0)`` that the ``instances`` row
      check runs, on THE SAME pid (``pane_pid_of`` feeds ``instances.pid``).

    Counting the second case as an independent witness alongside the registry is
    exactly the bug: one syscall, two hats, and a destruction authorised on it.
    So the caller needs to know WHICH of the two spoke, and this is what tells it.

    ``(None, None)`` — we could not look at all.
    """
    snapshot = _observed_snapshot(socket_name, snapshot_fn, in_sif_fn)
    if snapshot is None:
        return None, None
    return True, session_name in snapshot


def session_name_for_config(config: Any) -> str:
    """The tmux session name sac owns for this agent (``tui-<name>``).

    Goes through the runtime's own canonical helper so the probe and the runtime
    can never drift apart about which session belongs to whom.
    """
    try:
        from ..runtimes.tui_session import session_name_for

        return str(session_name_for(config))
    except Exception:  # stx-allow: fallback (an unresolvable session name must not crash a liveness read; the caller degrades to the ambiguous-instrument branch, which COLLAPSES rather than inventing a witness)
        return f"tui-{getattr(config, 'name', '')}"
