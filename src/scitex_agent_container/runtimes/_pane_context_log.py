"""Report a pane-backed fault: one loud line, a DURABLE snapshot, a quiet tail.

Two rules, learned from two different operator reports, pull in opposite
directions. This module is where they are reconciled, so every fault path can
obey both by calling one function.

**Rule 1 — a pane tail is CONTEXT, not a fault** (operator, 2026-07-23). A pane
tail is a transcription of ANOTHER session's screen. Carried inside the error
record that reports a boot fault it inherits that record's level, and the log
formatter stamps the level onto EVERY line — so a start that succeeded scrolls
past as::

    sac-start ERRO: TuiSessionRuntime: stale compose buffer ... Pane tail:
    ERRO: ✻ Running scheduled task (Jul 23 1:25am)
    ERRO: ❯ /compact
    SUCC: grant started

Fourteen lines of someone else's UI, all labelled as failures, with the one line
that IS the fault indistinguishable among them. So the transcription is logged
as its own INFO record.

**Rule 2 — the evidence must SURVIVE that demotion** (operator, 2026-08-08,
card ``sac-boot-failure-drops-its-own-pane-evidence-20260808``). Rule 1 alone
throws the evidence away: ``sac-start`` renders at a level that shows ERRO and
drops INFO, so the operator got two boot faults and no screen at all::

    ERRO: TuiSessionRuntime: stale compose buffer for tui-... did NOT clear ...
    ERRO: TuiSessionRuntime: startup_prompt ... stayed pasted-but-UNSENT ...
    SUCC: scitex-agent-container started

「まあやはりまずはログを取ることからかと。ターミナルのスナップショットぐらい
取れると思うんですよね。」 — and the tail was only ever 14 rows anyway, a tail
rather than a snapshot.

:func:`log_pane_fault` satisfies both: it writes the FULL pane to a file first,
names that file INSIDE the single-line fault record (so the loud line is
self-sufficient at any console level), then logs the tail at INFO for the reader
who is already looking. A write that fails says so in that same loud line — a
snapshot that silently did not happen would be worse than none, because the
absence would read as "there was nothing to see".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Rendered when the caller has no pane to show. A pane tail that is empty
#: because the session was already gone is evidence too, and silently logging
#: a blank record would read as "nothing was wrong here".
NO_PANE_CAPTURED = "(nothing captured)"

#: Directory under the runtime base that collects every pane snapshot, one
#: append-only file per tmux session. Deliberately NOT each agent's own state
#: dir: that dir is project-scope dependent (see
#: ``tui_session.state_dir_for_config``) and cannot be resolved from a session
#: name alone, so guessing it would sometimes write the evidence somewhere
#: other than where this docstring promises. One predictable tree, greppable
#: across every agent, is the honest trade.
SNAPSHOT_SUBDIR = "pane-faults"

_PANE_CONTEXT_TEMPLATE = (
    "TuiSessionRuntime: pane tail for %s — a copy of that session's screen, "
    "logged as context for the message above (not itself a fault):\n%s"
)

#: Appended to every fault message so the loud record names its own evidence.
_SNAPSHOT_SUFFIX = " Full pane snapshot: %s"


@dataclass(frozen=True)
class PaneSnapshot:
    """Where a pane snapshot was meant to land, and whether it did.

    Three-valued on purpose: ``path`` set means it landed, ``error`` set means
    it did not and says why, and ``target`` is populated either way so a
    failure can still tell the reader WHERE to look (a bare ``None`` would
    leave the fault line unable to say anything useful).
    """

    target: Path
    path: Path | None = None
    error: str | None = None

    def describe(self) -> str:
        """One-line rendering for the fault record — never empty."""
        if self.path is not None:
            return str(self.path)
        return f"NOT SAVED to {self.target} ({self.error or 'unknown error'})"


def snapshot_path_for(session_name: str, *, root: Path | None = None) -> Path:
    """Return the append-only snapshot file for ``session_name``.

    ``root`` defaults to the sac runtime base, so relocating the runtime tree
    (``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``) moves the snapshots with it rather
    than stranding them on a full filesystem.
    """
    if root is None:
        from .._runtime_paths import runtime_base_dir

        root = runtime_base_dir()
    # A session name reaches us from a caller, not from a user prompt, but it
    # ends up as a filename — so strip anything that could climb out of the
    # snapshot dir rather than trusting the caller to have been careful.
    safe = session_name.replace("/", "_").replace("\\", "_").strip(".") or "unnamed"
    return Path(root) / SNAPSHOT_SUBDIR / f"{safe}.log"


def write_pane_snapshot(
    session_name: str,
    pane: str,
    *,
    root: Path | None = None,
    time_fn: Callable[[], float] = time.time,
) -> PaneSnapshot:
    """Append the FULL captured pane, under a timestamped header.

    Append rather than one-file-per-fault: a boot typically trips more than one
    fault, and reading them in order in a single file is how the sequence
    ("the clear gave up, THEN the submit never landed") becomes visible.

    Never raises. A failure to record evidence must not replace the fault the
    caller was in the middle of reporting — it is returned instead, and
    :func:`log_pane_fault` prints it on the same loud line.
    """
    target = snapshot_path_for(session_name, root=root)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time_fn()))
    body = pane if pane else NO_PANE_CAPTURED
    header = f"===== {stamp} {session_name} ====="
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(f"{header}\n{body}")
            if not body.endswith("\n"):
                fh.write("\n")
        return PaneSnapshot(target=target, path=target)
    except OSError as exc:
        return PaneSnapshot(target=target, error=f"{type(exc).__name__}: {exc}")


def pane_tail(pane: str, lines: int = 14) -> str:
    """Last ``lines`` non-empty rows of a captured pane, for loud diagnostics.

    A boot-drain failure logs this so the operator sees the EXACT modal /
    login-wall / render state that blocked readiness — never a bare
    "timed out" with no evidence.
    """
    rows = [r for r in (pane or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def log_pane_context(
    log: logging.Logger,
    name: str,
    pane: str,
    *,
    lines: int = 14,
) -> None:
    """Log ``pane``'s tail for session ``name`` at INFO, as its own record."""
    tail = pane_tail(pane, lines)
    log.info(_PANE_CONTEXT_TEMPLATE, name, tail if tail else NO_PANE_CAPTURED)


def log_pane_fault(
    log: logging.Logger,
    name: str,
    pane: str,
    message: str,
    *args: Any,
    lines: int = 14,
    root: Path | None = None,
    write_fn: Callable[..., PaneSnapshot] = write_pane_snapshot,
) -> PaneSnapshot:
    """Report one pane-backed fault, with its evidence made durable.

    ``message`` is the caller's own ``%``-style fault template and ``args`` its
    arguments — unchanged, so each fault keeps saying what only it can say.
    This function appends the snapshot location to that single line, then logs
    the tail as INFO context.

    Order matters: the snapshot is written BEFORE the fault is logged, so the
    loud line can name a file that already exists.
    """
    snapshot = write_fn(name, pane, root=root)
    log.error(message + _SNAPSHOT_SUFFIX, *args, snapshot.describe())
    log_pane_context(log, name, pane, lines=lines)
    return snapshot


__all__ = [
    "NO_PANE_CAPTURED",
    "SNAPSHOT_SUBDIR",
    "PaneSnapshot",
    "log_pane_context",
    "log_pane_fault",
    "pane_tail",
    "snapshot_path_for",
    "write_pane_snapshot",
]
