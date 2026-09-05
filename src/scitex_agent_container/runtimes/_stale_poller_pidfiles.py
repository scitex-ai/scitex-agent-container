"""Clear telegrammer poller pidfiles left by a PREVIOUS container incarnation.

Why this exists (measured 2026-09-05, scitex-compute-04, agent scitex-cards):
claude-code-telegrammer keeps a per-bot-token pidfile in the agent's state dir
(``~/.scitex/claude-code-telegrammer/runtime/<agent>/poller-<token>.pid``), and
that dir lives in the overlay home, which survives a container restart. The
container's pid namespace does not. A restarted container hands its low pids out
again within seconds, in the very burst that spawns the MCP servers, so the pid
in the stale file was reassigned - to the telegram MCP server itself - and the
new poller's takeover SIGTERMed it. Two restarts in one day died that way; the
one that lived had read a pid above the burst.

The telegrammer is being fixed to verify identity before it signals. This guard
closes the window from sac's side, and it is correct on its own terms: sac only
enters the start path when the agent is NOT running, and the previous
container's poller died with that container, so at this moment a pidfile here
can only name a pid the new namespace is about to reuse. It never describes a
live process. Removing it turns the poller's next start into the
"no prior poller recorded" path, which signals nothing.
"""

from __future__ import annotations

from pathlib import Path

from .._logging import get_logger

__all__ = [
    "POLLER_PIDFILE_GLOB",
    "SERVER_LOCKFILE_NAME",
    "TELEGRAMMER_RUNTIME_REL",
    "clear_stale_poller_pidfiles",
]

#: Where claude-code-telegrammer keeps its per-agent state, relative to $HOME.
TELEGRAMMER_RUNTIME_REL = Path(".scitex") / "claude-code-telegrammer" / "runtime"
#: One file per bot token, written by lib/takeover.ts.
POLLER_PIDFILE_GLOB = "poller-*.pid"
#: The MCP server's single-instance lock (lib/lock.ts) - the same "newest wins,
#: SIGTERM the recorded pid" protocol, so the same stale-namespace hazard.
SERVER_LOCKFILE_NAME = "claude-code-telegrammer-mcp.lock"

_log = get_logger(__name__)


def clear_stale_poller_pidfiles(home: Path) -> list[Path]:
    """Remove every telegrammer pid record under ``home``; return what was removed.

    Two kinds, both written as "<pid>" by a previous incarnation and both
    consumed by a "newest wins" takeover that signals the recorded pid: the
    per-token poller pidfile and the MCP server's single-instance lock.

    Runs at launch, before the harness starts. Missing directories are a
    no-op. Every removal is logged by name so a reader of the boot log can
    see which previous incarnation's record was retired.
    """
    runtime = Path(home) / TELEGRAMMER_RUNTIME_REL
    if not runtime.is_dir():
        return []
    removed: list[Path] = []
    candidates = sorted(runtime.glob(f"*/{POLLER_PIDFILE_GLOB}")) + sorted(
        runtime.glob(f"*/{SERVER_LOCKFILE_NAME}")
    )
    for pidfile in candidates:
        try:
            pidfile.unlink()
        except FileNotFoundError:
            continue
        removed.append(pidfile)
        _log.info(
            "launch: retired a poller pidfile from a previous container incarnation: %s",
            pidfile,
        )
    return removed
