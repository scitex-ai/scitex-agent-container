"""Is an agent's INBOUND operator rail actually attached?

MEASURED 2026-08-03: of ten live agents, every one declared
``server:claude-code-telegrammer`` in its spec and FIVE could not receive
operator Telegram at all. Two distinct causes, one shared symptom:

* four had no bot provisioned — the pool holds 18 ``CCT_BOT_TOKEN_<SLOT>``
  names and none matched them, so the resolver emitted a WARNING and the
  agent ran on
* one (scitex-hub) had a valid token, correct config, and an env identical in
  shape to the working agents, and its server child was simply absent anyway

WHY THIS NEEDS A HOST-SIDE DETECTOR, and not a nicer warning:

**An agent cannot self-diagnose this.** An absent MCP tool is
indistinguishable from an absent message — there is no error, no failed call,
no log line, because the tool is not there to fail. scitex-hub would have
reported itself healthy all night, and every signal we already collect agreed
with it: tmux session alive, a2a channel adapter running, heartbeat fresh, two
processes up 3h23m. The operator asked three times why an agent was not
answering; the fleet's own instruments said nothing was wrong.

The existing control is a WARNING at slot-resolution time. That is a control
that fires into a log nobody reads, which is exactly how a 50%-dead rail
survived unnoticed. See [[reference-the-control-that-exists-but-never-fires]].

WHAT THIS MEASURES, precisely: whether the agent's ``claude`` process has a
live telegram-server CHILD. That is the thing whose absence *is* the outage —
it catches the no-token case and the died-anyway case identically, because it
observes the effect rather than any of the several causes.

THREE-VALUED ON PURPOSE. ``None`` when we cannot look (no pid recorded, an
unreadable ``/proc`` entry, a foreign host). A detector that reports "detached"
because it could not read ``/proc`` would manufacture exactly the false alarm
that trains people to ignore it — and this fleet has already deleted live
agents by collapsing an unknown into a negative. See
[[reference-binary-where-ternary-was-needed]].
"""

from __future__ import annotations

from pathlib import Path

#: Substring identifying the telegram MCP server in a child's cmdline. The
#: server is a bun/TypeScript process spawned by ``claude`` — NOT a Python
#: module, which is why ``import claude_code_telegrammer`` fails inside every
#: container, working ones included. Matching the script name rather than the
#: interpreter keeps this true if the runner changes.
_TELEGRAM_SERVER_MARKER = "telegram-server"

ATTACHED = "attached"
DETACHED = "detached"


def rail_state_from_cmdlines(child_cmdlines) -> str:
    """``attached`` when any child looks like the telegram server.

    Pure and total over its input — the injection seam the tests drive, so no
    test needs to invent a ``/proc``.
    """
    for cmdline in child_cmdlines or ():
        if _TELEGRAM_SERVER_MARKER in str(cmdline):
            return ATTACHED
    return DETACHED


def _child_pids(pid: int, proc_root: Path) -> list[int]:
    """PIDs listed in ``/proc/<pid>/task/*/children``, or ``[]``."""
    out: list[int] = []
    task_dir = proc_root / str(pid) / "task"
    # stx-allow: fallback (reason: an unreadable /proc entry is UNKNOWN and is
    # handled by the caller returning None -- never by reporting "detached".)
    try:
        for task in task_dir.iterdir():
            children = (task / "children").read_text().split()
            out.extend(int(c) for c in children)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return []
    return out


def _cmdline(pid: int, proc_root: Path) -> str:
    # stx-allow: fallback (reason: a process that exits mid-scan is normal;
    # its absence is not evidence about the rail.)
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def inbound_rail_state(pid, *, proc_root: Path | None = None) -> str | None:
    """``attached`` / ``detached`` / ``None`` when UNKNOWABLE.

    ``None`` for a missing pid or an unreadable ``/proc`` — the caller renders
    that as unknown, never as a fault.
    """
    if not pid:
        return None
    root = proc_root or Path("/proc")
    if not (root / str(int(pid))).exists():
        return None
    kids = _child_pids(int(pid), root)
    if not kids:
        # The agent's own process is readable but lists no children at all.
        # That is a real observation (a claude with no MCP children), not a
        # read failure, so it is a genuine DETACHED rather than unknown.
        return DETACHED
    return rail_state_from_cmdlines(_cmdline(k, root) for k in kids)
