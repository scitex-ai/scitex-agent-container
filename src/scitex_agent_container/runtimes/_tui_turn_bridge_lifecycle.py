"""Launcher / lifecycle for the TUI A2A turn bridge (mirrors ``a2a_sidecar``).

Extracted from :mod:`_tui_turn_bridge` (module line cap) — the spawn/teardown
machinery for the per-agent host-side ``/v1/turn`` bridge. The HTTP server +
subprocess entry point stay in :mod:`_tui_turn_bridge`, which RE-EXPORTS the
names here so ``_tui_turn_bridge.start_turn_bridge`` / ``stop_turn_bridge`` /
``resolved_a2a_port`` / ``_pid_path`` remain the public surface.

Dependency direction is one-way (``_tui_turn_bridge`` → this module): the
launcher spawns ``python -m <MODULE_PATH>`` by string (never importing the HTTP
server), so there is no import cycle.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..config import AgentConfig
from ._tui_turn_bridge_port import (
    _PORT_FREE_TIMEOUT_S,
    _STOP_SIGTERM_GRACE_S,
    TurnBridgePortBusyError,
    await_bridge_release,
    ensure_port_free_or_raise,
    force_free_own_port,
    port_busy_error,
    port_is_free,
)

log = logging.getLogger(__name__)

PID_FILENAME = "tui-turn-bridge.pid"
LOG_FILENAME = "tui-turn-bridge.log"
# The ``python -m`` target for the spawned bridge process. Points at the SIBLING
# module (which owns ``main()`` / ``__main__``), not this one — the launcher
# spawns it by string so this module never imports the HTTP server.
MODULE_PATH = "scitex_agent_container.runtimes._tui_turn_bridge"
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# Port resolution
# ---------------------------------------------------------------------------
def resolved_a2a_port(config: AgentConfig) -> int | None:
    """Return the agent's resolved a2a port as a positive int, else None.

    By the time the runtime starts, ``sac agents start`` has resolved a
    ``spec.a2a.port: auto`` to a concrete int (the SAME value threaded
    into the channel subscriber's ``--turn-url`` — see
    ``_apptainer_inner_argv.tui_channel_config``), so the bridge binds the
    port the subscriber will POST to. Returns None when a2a is unset or
    still unresolved (caller no-ops — no endpoint to serve).
    """
    a2a = getattr(config, "a2a", None)
    port = getattr(a2a, "port", None) if a2a is not None else None
    if isinstance(port, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(port, int) and port > 0:
        return port
    return None


def resolved_a2a_host(config: AgentConfig) -> str:
    """Return the agent's declared ``spec.a2a.host``, else :data:`DEFAULT_HOST`.

    The bridge is the TUI runtime's half of the SAME ``/v1/turn`` endpoint the
    SDK runner serves, so it must bind the SAME address the spec declares —
    ``runtimes/a2a_sidecar.py`` already reads ``spec.a2a.host`` for its
    ``a2a serve --host``. This module previously hardcoded :data:`DEFAULT_HOST`,
    so a spec asking for a reachable address bound loopback here anyway and
    nothing reported the disagreement: the spec said one thing, the runtime did
    another, and ``sac agents health`` was happy either way.

    A missing / blank / non-string host falls back to :data:`DEFAULT_HOST` —
    the same value every other reader defaults to — so a spec that declares
    nothing binds exactly where it bound before.
    """
    a2a = getattr(config, "a2a", None)
    host = getattr(a2a, "host", None) if a2a is not None else None
    if isinstance(host, str) and host.strip():
        return host.strip()
    return DEFAULT_HOST


# ---------------------------------------------------------------------------
# Launcher / lifecycle
# ---------------------------------------------------------------------------
def _state_dir(config: AgentConfig) -> Path:
    from .tui_session import state_dir_for_config

    return state_dir_for_config(config)


def _pid_path(config: AgentConfig) -> Path:
    return _state_dir(config) / PID_FILENAME


def start_turn_bridge(
    config: AgentConfig,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    host: str | None = None,
    port_free_timeout_s: float = _PORT_FREE_TIMEOUT_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    port_free_fn: Callable[[str, int], bool] = port_is_free,
) -> int | None:
    """Spawn the detached turn bridge for ``config``; return its PID or None.

    No-op (returns None) without a resolved ``a2a.port`` (nothing to serve).
    Best-effort spawn: a failure is logged + swallowed (a dead bridge must
    not block agent start); the ``spawn`` seam lets tests assert the argv.

    We ALWAYS :func:`stop_turn_bridge` first (one-bridge-per-agent invariant)
    AND then POLL until the a2a port is bindable before spawning. On the
    2026-07-09 restart port-collision incident the old holder still owned the
    port when the new child bound it → ``EADDRINUSE`` crash → agent stranded.
    If the port cannot be freed within ``port_free_timeout_s`` we FAIL LOUD
    (:class:`TurnBridgePortBusyError`, naming port + holder + remediation)
    instead of spawning into that crash. ``*_fn`` seams are injected by tests.

    ``host`` defaults to the agent's declared ``spec.a2a.host``
    (:func:`resolved_a2a_host`) — the SAME address ``a2a_sidecar`` binds — and
    is threaded into the spawned bridge's ``--host`` as well as into the
    port-free probes, which must ask about the address we are about to bind.
    An explicit ``host=`` still wins (test seam / caller override).
    """
    port = resolved_a2a_port(config)
    if port is None:
        return None
    if host is None:
        host = resolved_a2a_host(config)
    # Tear down any prior bridge for THIS agent, now also WAITING for it to
    # release the port. ``force_free_survivors=False`` — at START the port's
    # holder is UNKNOWN (could be a foreign service that grabbed the configured
    # port, not our orphan), so we must NOT force-kill it here; the
    # ``ensure_port_free_or_raise`` gate below is the deliberate REFUSE-and-warn
    # for that case. Auto-killing the own-port holder is safe only on the
    # genuine STOP path (see ``force_free_own_port``'s safety invariant).
    stop_turn_bridge(
        config,
        host=host,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        port_free_fn=port_free_fn,
        force_free_survivors=False,
    )
    # Bounded wait for the port to be bindable BEFORE we spawn — covers the
    # async shutdown tail AND an untracked/orphaned holder. Fails loud instead
    # of spawning a child into an EADDRINUSE crash.
    ensure_port_free_or_raise(
        host=host,
        port=port,
        agent_name=str(getattr(config, "name", "?")),
        timeout_s=port_free_timeout_s,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        port_free_fn=port_free_fn,
    )
    config_path = str(getattr(config, "config_path", "") or "")
    if not config_path:
        log.warning(
            "tui-turn-bridge: agent %r has no config_path; cannot start bridge",
            getattr(config, "name", "?"),
        )
        return None
    state_dir = _state_dir(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        MODULE_PATH,
        "--config-path",
        config_path,
        "--port",
        str(port),
        "--host",
        host,
    ]
    try:
        log_fh = open(state_dir / LOG_FILENAME, "ab")
        proc = spawn(
            argv,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # stx-allow: fallback (reason: best-effort sidecar — a spawn failure must not wedge agent start; logged for the operator)
        log.warning("tui-turn-bridge: failed to spawn for %r: %s", config.name, exc)
        return None
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        _pid_path(config).write_text(str(pid), encoding="utf-8")
    log.info(
        "tui-turn-bridge: started for %s on %s:%d (pid=%s)",
        config.name,
        host,
        port,
        pid,
    )
    return pid


def stop_turn_bridge(
    config: AgentConfig,
    *,
    host: str | None = None,
    grace_s: float = _STOP_SIGTERM_GRACE_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    port_free_fn: Callable[[str, int], bool] = port_is_free,
    force_free_survivors: bool = True,
) -> bool:
    """SIGTERM the recorded bridge, WAIT for it to RELEASE THE PORT (SIGKILL
    if it overstays ``grace_s``), sweep any OWN-port survivor, then drop the PID
    file; return True iff a live PID was signalled. No-op (returns False) when
    no PID file exists.

    Incident fix (2026-07-09): the old code SIGTERMed and returned
    IMMEDIATELY — the port stayed held during the async ``serve_forever``
    shutdown, so a fast ``restart`` rebound straight into ``EADDRINUSE`` and
    stranded the agent. :func:`await_bridge_release` blocks until the port is
    bindable again (accurate even for a not-yet-reaped zombie, which holds no
    socket; falls back to PID liveness when the agent has no a2a port).

    Incident fix (2026-07-12): ``await_bridge_release`` only reaps the ONE
    bridge PID sac TRACKED. A DIFFERENT holder of the SAME port — an orphaned
    prior bridge, or a process tied to the ``tui-<name>`` session (the operator
    saw survivor PID 2170086) — kept the port, so the next start's
    ``ensure_port_free_or_raise`` failed loud and the operator had to run
    ``fuser -k <port>/tcp`` by hand. With ``force_free_survivors`` (default True
    on the genuine STOP path) we now :func:`force_free_own_port` — the in-process
    ``fuser -k`` — SIGKILLing whatever still holds the agent's OWN resolved a2a
    port and re-polling. This is safe ONLY here (a KNOWN agent being torn down);
    ``start_turn_bridge`` passes ``force_free_survivors=False`` so the START gate
    keeps REFUSING an unknown holder instead of killing a stranger. If even the
    SIGKILL sweep cannot free the port we PRESERVE FAIL-LOUD (raise
    :class:`TurnBridgePortBusyError`) rather than let the next start crash on
    ``EADDRINUSE``. The PID file is removed regardless. ``*_fn`` seams are
    injected for tests.

    ``host`` defaults to the agent's declared ``spec.a2a.host``
    (:func:`resolved_a2a_host`) so the port-release probes ask about the
    address the bridge actually bound; an explicit ``host=`` still wins.
    """
    if host is None:
        host = resolved_a2a_host(config)
    pid_path = _pid_path(config)
    if not pid_path.is_file():
        return False
    agent_name = str(getattr(config, "name", "?"))
    stopped = False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: an unreadable/corrupt PID file is treated as "already stopped"; we still unlink it below so the next start is clean)
        pid = -1
    port = resolved_a2a_port(config)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            stopped = False
        except OSError as exc:  # stx-allow: fallback (reason: a permission/ESRCH error still means "not our live process"; log + treat as stopped so cleanup proceeds)
            log.warning("tui-turn-bridge: SIGTERM pid %d failed: %s", pid, exc)
        if stopped:
            # Block until the port is released (SIGKILL if SIGTERM ignored),
            # so a fast restart never rebinds into a still-held port.
            await_bridge_release(
                pid,
                port,
                host=host,
                grace_s=grace_s,
                sleep_fn=sleep_fn,
                now_fn=now_fn,
                port_free_fn=port_free_fn,
            )
    # Survivor sweep — the ``fuser -k`` equivalent for a holder that is NOT the
    # tracked PID (see the docstring's 2026-07-12 incident). SAFETY: only ever
    # the agent's OWN resolved a2a ``port``, only on the genuine STOP path
    # (``force_free_survivors`` default True; ``start_turn_bridge`` opts out).
    pending_error: TurnBridgePortBusyError | None = None
    if force_free_survivors and port is not None:
        if not force_free_own_port(
            port,
            host=host,
            agent_name=agent_name,
            grace_s=grace_s,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            port_free_fn=port_free_fn,
        ):
            # PRESERVE FAIL-LOUD: even after SIGKILLing the holder(s) the port
            # will not free — surface the actionable error (holder + one-liner
            # remediation) instead of letting the next start hit EADDRINUSE.
            pending_error = port_busy_error(host, port, agent_name)
    try:
        pid_path.unlink()
    except OSError:  # stx-allow: fallback (reason: unlink race is harmless — the file is gone either way)
        pass
    if pending_error is not None:
        raise pending_error
    return stopped


__all__ = [
    "DEFAULT_HOST",
    "LOG_FILENAME",
    "MODULE_PATH",
    "PID_FILENAME",
    "resolved_a2a_host",
    "resolved_a2a_port",
    "start_turn_bridge",
    "stop_turn_bridge",
    "_pid_path",
    "_state_dir",
]
