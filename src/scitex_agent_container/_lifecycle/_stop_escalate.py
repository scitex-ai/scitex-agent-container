"""The restart's stop leg, made honest: SIGTERM → SIGKILL → fail loud.

Extracted from :mod:`._stop` (512-line cap) so the whole "is the previous
runtime ACTUALLY down?" decision lives in ONE testable place.

THE BUG THIS CLOSES (operator terminal, 2026-07-14, card
``sac-restart-prints-success-after-start-failed``)::

    WARN: previous runtime still running after 15.00s (SIGTERM ignored...);
          proceeding to start anyway.
    FAIL: duplicate session 'tui-neurovista' — agent already running.
    Agent 'neurovista' restarted        <-- IT WAS NOT

Read those three lines in order. The stop leg PREDICTED the collision
("still running"), shrugged, and walked into it; the collision then
happened exactly as predicted; and the restart reported success over an
agent that was left DOWN. The gate had all the information needed to stop
— and used it only to narrate the failure it was about to cause.

The old ``_wait_for_previous_runtime_to_exit`` returned ``False`` on
timeout and its ONLY caller (``agent_restart``) discarded the value. A
verdict nobody reads is not a verdict.

WHY ESCALATE (SIGKILL) RATHER THAN ONLY FAIL LOUD
-------------------------------------------------
A restart's contract is to REPLACE the process, and the standard contract
for a forced stop everywhere else in the industry is SIGTERM, a grace
window, then SIGKILL (systemd ``TimeoutStopSec``, ``docker stop``,
kubelet). sac already had the SIGTERM and the 15 s grace; the escalation
step was simply missing. Refusing to escalate would mean sac cannot
restart an agent whose claude TUI is wedged mid-render and no longer
reaping signals — which is precisely the population that most needs a
restart. So: escalate FIRST, and fail loud only when the escalation
itself cannot be confirmed.

Both halves are required. Escalation without the fail-loud is just a
longer path to the same lie (SIGKILL can fail: no resolvable pid on a
remote/containerised runtime, an EPERM, or a process wedged in
uninterruptible-``D`` I/O where SIGKILL cannot land until the syscall
returns). Fail-loud without escalation would abort restarts that a single
``kill -9`` would have fixed.

KILL THE RIGHT PROCESS
----------------------
:meth:`RuntimeBase.agent_pid` — NOT the launcher. For a TUI agent the
launcher spawns the tmux session and exits within seconds; the LONG-LIVED
process is the pane pid (the pane's ``bash -c`` ``exec``s apptainer, and
``exec`` keeps the pid). SIGKILLing a launcher pid would look right in the
log and do nothing — and, pids being reused, could kill an unrelated
process. ``agent_pid`` is contractually the same signal the runtime's own
``is_running`` keys its verdict on, so "the thing we killed" and "the thing
that is still running" cannot drift apart. A runtime that cannot name a
local pid (docker / SSHRemote) returns ``None`` — honestly unknown — and we
fail loud instead of guessing.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any, Callable, Optional

from ..config import AgentConfig, load_config
from ._runtime_select import _get_runtime

logger = logging.getLogger(__name__)

__all__ = [
    "StopEscalationError",
    "ensure_previous_runtime_down",
    "escalate_to_sigkill",
    "long_lived_pid",
]

# How long to wait for the runtime to read as stopped AFTER SIGKILL.
# SIGKILL is not negotiable, but reaping is not instantaneous: the kernel
# tears the process down and (for a TUI agent) tmux must then notice the
# pane died and close the session. 5 s is ~50x the observed teardown.
_DEFAULT_SIGKILL_SETTLE_S = 5.0
# Poll interval for both the SIGTERM grace and the post-SIGKILL settle.
_POLL_INTERVAL_S = 0.25


class StopEscalationError(RuntimeError):
    """The previous runtime survived SIGTERM *and* SIGKILL.

    Raised INSTEAD of proceeding into a start leg that is now GUARANTEED to
    collide with the survivor (duplicate session) — the collision the old
    code predicted in a WARN and then caused. Carries ``name`` / ``pid`` so
    a structured caller can act without re-parsing the message.
    """

    def __init__(self, message: str, *, name: str, pid: int | None = None) -> None:
        super().__init__(message)
        self.name = name
        self.pid = pid


def long_lived_pid(runtime: Any, config: AgentConfig) -> int | None:
    """The runtime's LONG-LIVED pid, or ``None`` when it cannot be named.

    Thin, defensive read of the :meth:`RuntimeBase.agent_pid` seam.
    ``None`` is an honest "unknown" and the caller MUST treat it as "cannot
    escalate" — never as "nothing to kill".

    Refuses to hand back a pid that must never be signalled:

      * ``pid <= 0`` — ``os.kill(0, SIGKILL)`` signals sac's ENTIRE process
        group and ``os.kill(-1, ...)`` signals every process this user owns.
        A runtime bug that returned 0 would otherwise turn one wedged agent
        into a fleet-wide massacre.
      * our own pid — sac would kill itself mid-restart.
    """
    getter = getattr(runtime, "agent_pid", None)
    if getter is None:
        return None
    try:
        pid = getter(config)
    except Exception:  # stx-allow: fallback (reason: a pid-probe hiccup is "unknown", never a fabricated pid — a wrong/reused pid would get an unrelated process SIGKILLed)
        logger.warning(
            "stop-escalation: agent_pid() probe failed for %r; cannot escalate",
            getattr(config, "name", "?"),
            exc_info=True,
        )
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if pid == os.getpid():
        logger.error(
            "stop-escalation: agent_pid() returned OUR OWN pid (%s) for %r — "
            "refusing to SIGKILL sac itself. This is a runtime bug.",
            pid,
            getattr(config, "name", "?"),
        )
        return None
    return pid


def _send_sigkill(pid: int, *, kill_fn: Callable[[int, int], None]) -> bool:
    """SIGKILL ``pid``. True iff the signal landed or the process was gone.

    ``ProcessLookupError`` is SUCCESS: the goal is "that process is not
    running", and it already is not. ``PermissionError`` / ``OSError`` are
    honest failures — we could not kill it, so we must not claim we did.
    """
    try:
        kill_fn(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        logger.error(
            "stop-escalation: not permitted to SIGKILL pid %s (owned by "
            "another user?) — cannot force the previous runtime down.",
            pid,
        )
        return False
    except OSError:
        logger.error(
            "stop-escalation: SIGKILL of pid %s failed", pid, exc_info=True
        )
        return False


def _poll_until_stopped(
    runtime: Any,
    config: AgentConfig,
    *,
    sleep_fn: Callable[[float], None],
    timeout_s: float,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> bool:
    """True as soon as ``runtime.is_running`` reads False; False on timeout."""
    deadline = time.monotonic() + timeout_s
    while runtime.is_running(config):
        if time.monotonic() >= deadline:
            return False
        sleep_fn(poll_interval_s)
    return True


def escalate_to_sigkill(
    name: str,
    config: AgentConfig,
    runtime: Any,
    *,
    sleep_fn: Callable[[float], None],
    settle_s: float = _DEFAULT_SIGKILL_SETTLE_S,
    kill_fn: Callable[[int, int], None] = os.kill,
) -> tuple[bool, int | None]:
    """SIGKILL the runtime's long-lived process; VERIFY it actually died.

    Returns ``(stopped, pid)``. ``stopped`` is the runtime's OWN verdict
    after the kill (``is_running`` read False within ``settle_s``) — never
    "we sent the signal, so it must be down". ``pid`` is what we killed, or
    ``None`` when the runtime could not name one (in which case no signal
    was sent and ``stopped`` reflects the runtime as it stands).
    """
    pid = long_lived_pid(runtime, config)
    if pid is None:
        logger.error(
            "stop-escalation: %r ignored SIGTERM and its runtime cannot name a "
            "long-lived pid (agent_pid() -> None), so there is nothing to "
            "SIGKILL. Not starting a replacement over a survivor.",
            name,
        )
        return runtime.is_running(config) is False, None
    logger.warning(
        "stop-escalation: %r ignored SIGTERM — escalating to SIGKILL on pid %s "
        "(the runtime's long-lived process, the same pid its is_running() keys "
        "on). This is the normal forced-stop contract: TERM, grace, KILL.",
        name,
        pid,
    )
    if not _send_sigkill(pid, kill_fn=kill_fn):
        return False, pid
    return (
        _poll_until_stopped(runtime, config, sleep_fn=sleep_fn, timeout_s=settle_s),
        pid,
    )


def _remedy(name: str, config: AgentConfig, pid: int | None) -> str:
    """Operator remedy that ACTUALLY works, sized to what we observed.

    Deliberately NOT ``sac agents restart`` — when this fires, that is the
    very command that just failed, so recommending it loops the operator
    back into the failure (the same reasoning as ``tui_session``'s
    duplicate-session guard).
    """
    lines: list[str] = []
    if pid is not None:
        lines.append(
            f"  ps -o pid,ppid,stat,wchan,cmd -p {pid}"
            f"   # STAT 'D' = uninterruptible I/O: SIGKILL cannot land until "
            f"the syscall returns"
        )
    # "" and "tui" both select TuiSessionRuntime (see _runtime_select).
    if (getattr(config, "runtime", "") or "").strip().lower() in ("", "tui"):
        lines.append(f"  tmux kill-session -t tui-{name}")
    lines.append(f"  sac agents start {name} -y --fresh")
    return "\n".join(lines)


def ensure_previous_runtime_down(
    name: str,
    config_path: str,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]],
    sleep_fn: Callable[[float], None],
    timeout_s: float,
    profile: str | None = None,
    settle_s: float = _DEFAULT_SIGKILL_SETTLE_S,
    kill_fn: Callable[[int, int], None] = os.kill,
) -> None:
    """Guarantee the previous runtime is DOWN, or raise.

    The restart's stop-leg gate. Returns normally ONLY when the runtime's
    own ``is_running`` reads False — i.e. when the start leg can safely
    replace it. Raises :class:`StopEscalationError` otherwise, so
    ``agent_restart`` never walks into the duplicate-session collision and
    never reports a restart that did not happen.

      1. Poll ``is_running`` for ``timeout_s`` (the SIGTERM grace the
         preceding ``agent_stop`` opened).
      2. Still up → SIGKILL the long-lived pid, poll again for ``settle_s``.
      3. STILL up → raise, loudly, with a remedy that works.

    ``timeout_s <= 0`` skips the gate entirely (legacy fixed-sleep
    behaviour, retained for callers that want the bypass), as does an
    unloadable spec — a YAML edited mid-restart must not block the restart;
    the new container surfaces the real parse error when it boots.
    """
    if timeout_s <= 0:
        sleep_fn(2)
        return
    # Load once: the config is stable across the gate, and re-loading per
    # poll would multiply YAML parsing on a busy host.
    try:
        config = load_config(config_path, profile=profile)
    except Exception:  # stx-allow: fallback (reason: YAML may have been edited mid-restart; fall back to the legacy fixed sleep instead of blocking the restart on a transient parse error — the new container surfaces the real error when it boots)
        sleep_fn(2)
        return
    factory = runtime_factory or _get_runtime
    runtime = factory(config)

    if _poll_until_stopped(runtime, config, sleep_fn=sleep_fn, timeout_s=timeout_s):
        return

    stopped, pid = escalate_to_sigkill(
        name,
        config,
        runtime,
        sleep_fn=sleep_fn,
        settle_s=settle_s,
        kill_fn=kill_fn,
    )
    if stopped:
        logger.warning(
            "stop-escalation: %r is down after SIGKILL (pid %s). The restart "
            "continues; the agent had to be force-killed, which means its "
            "runtime stop path did not honour SIGTERM — worth investigating "
            "(see ApptainerContainerRuntime.stop / TuiSessionRuntime.stop).",
            name,
            pid,
        )
        return

    killed = "SIGKILL" if pid is not None else "no pid to SIGKILL"
    raise StopEscalationError(
        f"restart of {name!r} ABORTED: the previous runtime is STILL RUNNING "
        f"after SIGTERM ({timeout_s:.1f}s grace) and {killed}"
        + (f" (pid {pid}, {settle_s:.1f}s settle)" if pid is not None else "")
        + f" — its own is_running() still reports True.\n"
        f"NOT starting a replacement: the start leg would collide with the "
        f"survivor (duplicate session {name!r}) and the restart would then "
        f"report success over a process that never died. {name!r} is still UP "
        f"as the OLD process on its OLD credentials — it was NOT restarted.\n"
        f"Diagnose, then recover:\n" + _remedy(name, config, pid),
        name=name,
        pid=pid,
    )
