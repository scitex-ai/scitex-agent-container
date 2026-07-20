"""Pre-start defence against orphaned MCP-server child processes.

Operator-listed top fleet failure mode (bug "MCP-on-restart 409 orphan"):
when an agent restarts, the previous ``claude-code-telegrammer`` poller
(or any stdio-MCP server child) sometimes survives the SIGTERM the SDK
sent it. The new poller then long-polls Telegram with ``getUpdates`` and
the Bot API returns HTTP 409 Conflict ("terminated by other getUpdates
request"). Operator-visible symptom: the operator sees "telegrammer
dead, agent alive but silent" — claude is running fine but no Telegram
inbound ever wakes it because the orphan stole the long-poll slot.

Two angles addressed this class of bug:

  1. Pre-stop SIGKILL after the SIGTERM grace — requires SDK cooperation
     (claude-agent-sdk owns the child PID handle; sac can only see it
     indirectly). Doable but coupled.
  2. Pre-start orphan scan — purely operator-side defence. Scan
     ``psutil.process_iter`` for processes whose env identifies them
     as belonging to *this* agent name AND whose cmdline looks like an
     MCP-server child, then SIGKILL them BEFORE the new runtime boots
     its replacement poller. This module implements (2).

Wired into :mod:`._start.agent_start` immediately before the
``runtime.start(config, ...)`` call, AFTER the singleton check and
forced-stop teardown. By that point any "expected" stdio child should
already be gone — anything still alive matching the agent name is an
orphan by definition.

Hard guarantees:

  * NEVER raises. Orphan cleanup is best-effort defence; a wedged
    psutil import / a permission-denied process must NOT block start.
    The caller proceeds whether this returns ``[]`` or a populated
    list.
  * No mocks in tests. The cleanup uses real :mod:`psutil` with a
    real process-iterator seam so tests can drive synthetic process
    snapshots without monkeypatching ``psutil`` internals.
  * Match by **two** signals — env var ``SCITEX_AGENT_CONTAINER_NAME``
    OR ``SAC_NAME`` equals ``name`` AND cmdline contains one of the
    MCP markers (``bun``, ``telegrammer``, ``mcp``, ``claude-code-
    telegrammer``). The env match alone is strong evidence the
    process belongs to this agent's last incarnation; the cmdline
    filter prevents the cleanup from killing unrelated children (eg
    a user-spawned editor that happened to inherit the env).
  * ``dry_run=True`` returns the would-kill PID list without actually
    sending SIGKILL — used by the test that does NOT want to touch
    real processes, and by future ``sac agents diagnose`` plumbing.
"""

from __future__ import annotations

import logging
import os
import signal
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

__all__ = ["kill_orphan_mcp_children", "MCP_CMDLINE_MARKERS"]


# Substrings (case-sensitive on Linux cmdlines, which are conventionally
# lowercase for these projects) we accept as evidence a process is an
# MCP-server child. Kept narrow on purpose — broadening to "claude" or
# "node" risks reaping the agent's own runtime or a shared interpreter.
MCP_CMDLINE_MARKERS: tuple[str, ...] = (
    "claude-code-telegrammer",
    "telegrammer",
    "bun",
    "mcp",
)

# Env-var names that the runtime injects identifying the owning agent.
# See ``_lifecycle/_start.py`` (``hook_env``) for the canonical write
# site of ``SCITEX_AGENT_CONTAINER_NAME``; ``SAC_NAME`` is the shorter
# alias used by the MCP-tool surface and inherited by stdio children.
_AGENT_NAME_ENV_KEYS: tuple[str, ...] = (
    "SCITEX_AGENT_CONTAINER_NAME",
    "SAC_NAME",
)


def _default_iter_processes() -> Iterable[Any]:
    """Real psutil.process_iter seam.

    Pulled out as the default for the ``process_iter`` injection point
    so tests can substitute a list of fake process objects without
    monkeypatching ``psutil`` itself. Defined as a function (not a
    bound method) so the import failure mode (no psutil installed) is
    handled inside :func:`kill_orphan_mcp_children` rather than at
    module import — sac itself must import cleanly on minimal hosts.
    """
    import psutil  # noqa: WPS433 — imported here so absence is defensive

    return psutil.process_iter(["pid", "cmdline", "environ"])


def _cmdline_str(proc: Any) -> str:
    """Best-effort cmdline string for ``proc``. ``""`` on any failure."""
    try:
        info = getattr(proc, "info", None)
        if info is not None and "cmdline" in info:
            parts = info["cmdline"] or ()
        else:
            parts = proc.cmdline() or ()
        return " ".join(str(p) for p in parts)
    except Exception:  # stx-allow: fallback (reason: psutil raises NoSuchProcess / AccessDenied / ZombieProcess for normal race conditions — treat as "no cmdline visible" and skip the process)
        return ""


def _environ(proc: Any) -> dict[str, str]:
    """Best-effort env-dict for ``proc``. ``{}`` on any failure."""
    try:
        info = getattr(proc, "info", None)
        if info is not None and "environ" in info:
            env = info["environ"] or {}
        else:
            env = proc.environ() or {}
        # psutil sometimes returns ``None`` for restricted procs.
        return dict(env) if env else {}
    except Exception:  # stx-allow: fallback (reason: psutil raises NoSuchProcess / AccessDenied / ZombieProcess for normal race conditions — treat as "no env visible" and skip the process)
        return {}


def _pid_of(proc: Any) -> int | None:
    """Best-effort ``pid`` for ``proc``. ``None`` on any failure."""
    try:
        info = getattr(proc, "info", None)
        if info is not None and "pid" in info:
            return int(info["pid"])
        return int(proc.pid)
    except Exception:  # stx-allow: fallback (reason: psutil raises NoSuchProcess for races between iter() and attribute access — skip the unknown-pid process)
        return None


def _looks_like_mcp_child(cmdline: str) -> bool:
    """True iff ``cmdline`` contains any known MCP-server marker."""
    if not cmdline:
        return False
    return any(marker in cmdline for marker in MCP_CMDLINE_MARKERS)


def _belongs_to_agent(env: dict[str, str], name: str) -> bool:
    """True iff ``env`` carries the agent-name env-var matching ``name``."""
    if not env:
        return False
    for key in _AGENT_NAME_ENV_KEYS:
        if env.get(key) == name:
            return True
    return False


def kill_orphan_mcp_children(
    name: str,
    dry_run: bool = False,
    *,
    process_iter: Callable[[], Iterable[Any]] | None = None,
    kill_fn: Callable[[int, int], None] | None = None,
    self_pid: int | None = None,
) -> list[int]:
    """Scan + SIGKILL orphaned MCP-server children of agent ``name``.

    Match policy: a process is an orphan iff BOTH
      * its env carries ``SCITEX_AGENT_CONTAINER_NAME`` or ``SAC_NAME``
        equal to ``name``, AND
      * its cmdline contains one of :data:`MCP_CMDLINE_MARKERS` (the
        narrow set of substrings we accept as MCP-server evidence).

    The current process is excluded so the helper can never kill the
    sac CLI itself (a paranoia guard — the CLI's env may legitimately
    carry the agent name during a ``sac agents start`` invocation).

    Args:
        name: Agent name (``config.name``) whose orphans we hunt.
        dry_run: When ``True``, returns the candidate PID list WITHOUT
            sending SIGKILL. Used by tests that must not touch real
            processes, and by future ``sac diagnose`` plumbing.
        process_iter: Real seam — callable returning an iterable of
            psutil-Process-shaped objects. Default is the real
            :func:`psutil.process_iter` (lazily imported so absence of
            psutil degrades to ``[]`` instead of crashing). Tests pass
            a callable returning a fixed list of fake processes.
        kill_fn: Real seam — callable invoked as ``kill_fn(pid, sig)``
            to SIGKILL the orphan. Default :func:`os.kill`. Tests pass
            a recorder that captures calls without touching the host.
        self_pid: Override for the "don't kill myself" guard. Defaults
            to :func:`os.getpid`. Tests pass an explicit value so the
            guard exercises deterministically against a fake snapshot.

    Returns:
        List of PIDs killed (or, with ``dry_run=True``, the PIDs that
        *would* have been killed). Empty list on any error / no
        orphans found — this function NEVER raises. The caller in
        ``agent_start`` does not check the return; the list exists for
        logging and for the test contract.
    """
    iter_fn = process_iter or _default_iter_processes
    killer = kill_fn or os.kill
    me = self_pid if self_pid is not None else os.getpid()

    # Top-level guard: psutil missing, iter() raising OSError, anything
    # weird during the snapshot — defensive return [], no raise.
    try:
        snapshot = list(iter_fn())
    except Exception:  # stx-allow: fallback (reason: the iter() call itself can fail when psutil is uninstalled, the /proc fs is restricted, or psutil raises OSError on a flaky host — orphan cleanup must NEVER wedge a restart)
        log.debug(
            "orphan_mcp_cleanup: process_iter failed for agent %r; skipping cleanup",
            name,
            exc_info=True,
        )
        return []

    killed: list[int] = []
    for proc in snapshot:
        pid = _pid_of(proc)
        if pid is None or pid == me:
            continue
        env = _environ(proc)
        if not _belongs_to_agent(env, name):
            continue
        cmdline = _cmdline_str(proc)
        if not _looks_like_mcp_child(cmdline):
            continue
        killed.append(pid)
        if dry_run:
            continue
        # stx-allow: fallback (reason: the orphan may have already exited between snapshot and kill — ESRCH / EPERM / OSError must not propagate; we logged the intent and move on)
        try:
            killer(pid, signal.SIGKILL)
            log.info(
                "orphan_mcp_cleanup: SIGKILL'd orphan MCP child pid=%d "
                "for agent %r (cmdline=%r)",
                pid,
                name,
                cmdline[:200],
            )
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment above)
            log.debug(
                "orphan_mcp_cleanup: failed to SIGKILL pid=%d for agent %r",
                pid,
                name,
                exc_info=True,
            )
    return killed
