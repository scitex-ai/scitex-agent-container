"""Port release + bind-collision guards for the TUI A2A turn bridge.

Extracted from :mod:`_tui_turn_bridge` (module line cap) — the machinery
that makes ``sac agents restart`` reliably RELEASE + REBIND the agent's
a2a/turn port.

The incident this fixes (2026-07-09, scitex-todo stranded): a restart
STOPPED the agent, but the new spawn failed with
``post_ack_no_apptainer_pid`` — the container never came up. The real
crash was in the child, logged to ``<runtime_dir>/tui-turn-bridge.log``::

    OSError: [Errno 98] Address already in use
      File ".../runtimes/_tui_turn_bridge.py", line ..., in __init__
        super().__init__(server_address, _TurnBridgeHandler)
      ...
        self.socket.bind(self.server_address)

The OLD instance's turn-bridge server was STILL holding the a2a port when
the new instance booted, so the new bind was refused. ``SO_REUSEADDR``
(``_TurnBridgeServer.allow_reuse_address``) covers only the ``TIME_WAIT``
fast-restart case — it does NOT let a bind steal a port from a
still-running listener, which is what bit us (the old process was alive,
not merely lingering). So the stop path must actually TERMINATE the old
holder and the start path must WAIT for the port to be bindable BEFORE it
spawns — and FAIL LOUD (naming the port + holder + the exact remediation)
if it cannot, rather than spawning straight into the crash.

Everything here is pure + unit-testable with REAL sockets/processes
(injectable ``sleep``/``now``/``port-free`` seams — no mocks):

* :func:`port_is_free` — a fresh ``SO_REUSEADDR`` bind probe answering
  "will the new bridge bind here right now?" faithfully.
* :func:`await_bridge_release` — wait for a SIGTERM'd bridge to release the
  port, escalating to SIGKILL past a grace (used by ``stop_turn_bridge``).
* :func:`port_busy_error` — the actionable :class:`TurnBridgePortBusyError`
  naming the port + holder PID(s) + the one-liner remediation.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
from typing import Callable

log = logging.getLogger(__name__)

# Bounded waits (seconds). ``_STOP_SIGTERM_GRACE_S`` — how long
# ``stop_turn_bridge`` waits for a SIGTERM'd bridge to release the port
# before escalating to SIGKILL. ``_PORT_FREE_TIMEOUT_S`` — how long
# ``start_turn_bridge`` polls for the port to become bindable before it
# FAILS LOUD (never spawns into a guaranteed crash). ``_PORT_POLL_INTERVAL_S``
# — poll cadence for both.
_STOP_SIGTERM_GRACE_S = 5.0
_PORT_FREE_TIMEOUT_S = 10.0
_PORT_POLL_INTERVAL_S = 0.2


class TurnBridgePortBusyError(RuntimeError):
    """The a2a port is still held when a new turn bridge must bind it.

    Raised (fail loud) by ``build_server`` when the bind is refused and by
    ``start_turn_bridge`` when the old holder could not be freed within the
    bounded wait. Carries ``port`` so a caller/log shows the exact
    remediation instead of a bare ``OSError [Errno 98]``.
    """

    def __init__(self, message: str, *, port: int) -> None:
        super().__init__(message)
        self.port = port


def _pid_alive(pid: int) -> bool:
    """``kill -0`` liveness. ``ProcessLookupError`` = reaped/dead.

    A not-yet-reaped zombie reads ALIVE here — which is why the PORT (a
    zombie holds no socket) is the primary release signal; PID liveness is
    only the fallback for an agent that declares no resolved a2a port.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # stx-allow: fallback (reason: any other errno on kill -0 means "not our live process"; treat as dead so teardown proceeds)
        return False
    return True


def port_is_free(host: str, port: int) -> bool:
    """True iff a fresh ``SO_REUSEADDR`` socket can bind ``host:port`` now.

    Mirrors the EXACT options ``_TurnBridgeServer`` uses
    (``allow_reuse_address`` → ``SO_REUSEADDR``), so a True result means
    "the new bridge WILL bind here": a still-running old listener makes
    ``bind`` fail with ``EADDRINUSE`` (SO_REUSEADDR never steals a LIVE
    listener), while a lingering ``TIME_WAIT`` socket is bindable (exactly
    what SO_REUSEADDR permits — the fast-restart case). The probe socket is
    never ``listen``-ed, so closing it leaves no ``TIME_WAIT`` of its own.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    poll_s: float,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
) -> bool:
    """Return True as soon as ``predicate()`` holds; False on timeout.

    Seams (``sleep_fn``/``now_fn``) are injected so a wait is unit-testable
    without real time; production passes ``time.sleep`` / ``time.monotonic``.
    """
    deadline = now_fn() + timeout_s
    while True:
        if predicate():
            return True
        if now_fn() >= deadline:
            return False
        sleep_fn(poll_s)


def _safe_port_holder_pids(port: int) -> list[int]:
    """Best-effort PID(s) LISTENING on ``port`` (lsof/ss/fuser); never raises.

    Reuses the tested :func:`.._listen._port_holder.port_holder_pids`
    finder so a fail-loud message can name the holder; an absent tool
    degrades to an empty list (the message still names the port +
    remediation).
    """
    try:
        from .._listen._port_holder import port_holder_pids

        return port_holder_pids(port)
    except Exception:  # stx-allow: fallback (reason: holder discovery is diagnostic only; a missing lsof/ss/fuser must not mask the loud bind error)
        return []


def port_busy_error(
    host: str, port: int, agent_name: str, *, cause: Exception | None = None
) -> TurnBridgePortBusyError:
    """Build the actionable :class:`TurnBridgePortBusyError` for ``port``.

    Names the holder PID(s) and the exact one-liner remediation
    (``fuser -k <port>/tcp; tmux kill-session -t tui-<name>``) so the
    operator recovers without diagnosing a bare ``EADDRINUSE``.
    """
    holders = _safe_port_holder_pids(port)
    who = (
        ", ".join(str(p) for p in holders)
        if holders
        else "unknown (install lsof/ss/fuser to identify)"
    )
    session = f"tui-{agent_name}"
    cause_txt = f" [{cause}]" if cause is not None else ""
    return TurnBridgePortBusyError(
        f"tui-turn-bridge: a2a port {port} on {host} for agent {agent_name!r} is "
        f"STILL held by an old holder{cause_txt} — refusing to bind into an "
        f"EADDRINUSE crash (the restart port-collision incident). Holder "
        f"PID(s): {who}; tmux session: {session}. Free it, then restart: "
        f"`fuser -k {port}/tcp; tmux kill-session -t {session}; "
        f"sac agents restart {agent_name}`.",
        port=port,
    )


def ensure_port_free_or_raise(
    *,
    host: str,
    port: int,
    agent_name: str,
    timeout_s: float,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
    port_free_fn: Callable[[str, int], bool] = port_is_free,
    poll_s: float = _PORT_POLL_INTERVAL_S,
) -> None:
    """Poll (bounded) until ``host:port`` is bindable; else FAIL LOUD.

    The pre-spawn gate for ``start_turn_bridge``: returns quietly once the
    port is free, otherwise logs + raises :class:`TurnBridgePortBusyError`
    (naming the port + holder + remediation) so the caller never spawns a
    child straight into an ``EADDRINUSE`` crash.
    """
    if poll_until(
        lambda: port_free_fn(host, port),
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    ):
        return
    err = port_busy_error(host, port, agent_name)
    log.error("%s", err)
    raise err


def await_bridge_release(
    pid: int,
    port: int | None,
    *,
    host: str,
    grace_s: float,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
    port_free_fn: Callable[[str, int], bool] = port_is_free,
    poll_s: float = _PORT_POLL_INTERVAL_S,
) -> bool:
    """Block until a SIGTERM'd bridge has RELEASED its port; SIGKILL if it
    overstays ``grace_s``, then wait once more. Returns True iff released.

    Uses the PORT as the release signal when the agent has a resolved a2a
    port (accurate even for a not-yet-reaped zombie — a dead process holds
    no socket); otherwise falls back to PID liveness. The SIGKILL branch is
    what makes a bridge that IGNORES SIGTERM / hangs its ``serve_forever``
    teardown actually give up the port before the caller rebinds.
    """

    def _released() -> bool:
        if port is not None:
            return port_free_fn(host, port)
        return not _pid_alive(pid)

    if poll_until(
        _released, timeout_s=grace_s, poll_s=poll_s, sleep_fn=sleep_fn, now_fn=now_fn
    ):
        return True
    # SIGTERM ignored / teardown hung past the grace — force it so the port
    # is actually released before the caller rebinds.
    try:
        os.kill(pid, signal.SIGKILL)
        log.warning(
            "tui-turn-bridge: pid %d ignored SIGTERM after %.1fs; sent SIGKILL "
            "to release port %s",
            pid,
            grace_s,
            port,
        )
    except OSError:  # stx-allow: fallback (reason: SIGKILL of an already-gone/foreign pid is a no-op; the release re-poll below is the real check)
        pass
    return poll_until(
        _released, timeout_s=grace_s, poll_s=poll_s, sleep_fn=sleep_fn, now_fn=now_fn
    )


__all__ = [
    "TurnBridgePortBusyError",
    "await_bridge_release",
    "ensure_port_free_or_raise",
    "poll_until",
    "port_busy_error",
    "port_is_free",
    "_STOP_SIGTERM_GRACE_S",
    "_PORT_FREE_TIMEOUT_S",
    "_PORT_POLL_INTERVAL_S",
]
