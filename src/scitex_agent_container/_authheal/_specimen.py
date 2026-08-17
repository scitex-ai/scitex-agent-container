"""Capture an agent's FULL live state to a file — BEFORE anything restarts it.

WHY THIS RUNS FIRST, ALWAYS
    A restart destroys the thing it fixes. The wedged process, its pane, its
    scrollback and the exact banner it was showing are the only reproduction we
    will ever have, and ``sac agents restart -y`` erases all of it in one call.
    On 2026-07-18 two live login-required agents were restarted before anyone
    captured them, and both reproductions were lost — the operator had ordered
    the capture explicitly, and it was skipped.

    So this is not diagnostics-on-the-side. It is a PRECONDITION of restarting:
    the pass captures, verifies the specimen actually landed on disk, and only
    then acts. A specimen that could not be written is a reason not to restart,
    exactly like an unwritable log — because the restart would then be both
    unrecorded and unreproducible.

WHAT IS CAPTURED
    Four independent readings, each labelled, each kept verbatim:

      1. the FULL ``sac agents auth-status`` table — the fleet-wide verdict at
         this instant, not just this agent's row, so the specimen shows what
         else was happening at the same moment;
      2. the pane PID and its ``ps`` line — the process identity, which is what
         later proves a restart really replaced the process (a new PID) rather
         than reporting success while nothing moved;
      3. the pane capture with scrollback (``-S -60``) — more than the visible
         pane, because the banner's HISTORY is what distinguishes an agent that
         just died from one that has been dead for hours;
      4. the ``agent_auth_state`` row — what sac's own cache believed.

    The format mirrors the operator's working example
    (``specimens/grant-20260718-222619.log``) verbatim, because that is the
    shape he has already read and asked for.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .._runners._tmux._target import exact_target

__all__ = [
    "Specimen",
    "capture_specimen",
    "specimen_dir",
    "specimen_path",
]

#: Scrollback depth. -60 keeps the banner's recent history, not just the frame.
_SCROLLBACK_LINES = 60

#: Per-command wall clock. Generous: a specimen is worth waiting for, and a slow
#: tmux is not a reason to restart an agent with no reproduction on disk.
_TIMEOUT_S = 60.0

_TUI_PREFIX = "tui-"


def specimen_dir() -> Path:
    """Where specimens live: ``<runtime>/specimens``, beside the other logs."""
    from .._state.state_paths import runtime_root

    return runtime_root() / "specimens"


def specimen_path(agent: str, *, now: datetime, root: Path | None = None) -> Path:
    """``<specimens>/<agent>-<YYYYmmdd-HHMMSS>.log`` — the operator's naming."""
    base = root if root is not None else specimen_dir()
    return base / f"{agent}-{now.strftime('%Y%m%d-%H%M%S')}.log"


@dataclass(frozen=True)
class Specimen:
    """Where the specimen landed, and whether it really did.

    ``ok=False`` means the capture did NOT reach disk. The caller must treat
    that as a blocker on restarting, never as a cosmetic warning: the whole
    point is that the reproduction exists BEFORE the evidence is destroyed.
    """

    path: Path
    ok: bool
    detail: str = ""
    text: str = ""


def _run(argv: list[str]) -> str:
    """Run a read-only probe and return its output, or a labelled failure.

    Never raises. A probe that fails is RECORDED as having failed — the specimen
    says which of the four readings is missing and why, instead of silently
    containing three readings and looking complete.
    """
    # stx-allow: fallback (reason: every failure mode must become TEXT INSIDE the
    # specimen — a missing section that says why it is missing is evidence; an
    # exception that aborts the capture destroys the whole reproduction)
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except Exception as exc:  # stx-allow: fallback (reason: see comment above)
        return f"<probe failed: {argv!r}: {exc.__class__.__name__}: {exc}>"
    body = out.stdout or ""
    if out.returncode != 0:
        body += f"\n<rc={out.returncode}>\n{out.stderr or ''}"
    return body


def _pane_pid(agent: str) -> str:
    return _run(
        [
            "tmux",
            "list-panes",
            "-t",
            exact_target(f"{_TUI_PREFIX}{agent}"),
            "-F",
            "#{pane_pid}",
        ]
    ).strip()


def _ps_line(pid: str) -> str:
    if not pid.isdigit():
        return f"<no numeric pane pid: {pid!r}>"
    return _run(["ps", "-o", "pid,lstart,etime,cmd", "-p", pid])


def _auth_status_table(sac_bin: str) -> str:
    """The FULL auth-status table — the detector's own output, not a re-derivation.

    Shelling out to the real command (rather than importing its internals) is
    deliberate: the specimen should contain exactly what the operator would have
    seen had he run it by hand at that instant.
    """
    return _run([sac_bin, "agents", "auth-status"])


def _state_db_row(agent: str) -> str:
    """The ``agent_auth_state`` row, pipe-separated as in the operator's example."""
    # stx-allow: fallback (reason: the cache is one of four readings; a missing
    # or unreadable state.db must be RECORDED in the specimen, not abort it)
    try:
        import sqlite3

        from .._state.state_db import DEFAULT_DB_PATH

        with sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT name, auth_failed, checked_at, banner, reason, note "
                "FROM agent_auth_state WHERE name=?",
                (agent,),
            ).fetchone()
    except Exception as exc:  # stx-allow: fallback (reason: see comment above)
        return f"<state.db unreadable: {exc.__class__.__name__}: {exc}>"
    if row is None:
        return f"<no agent_auth_state row for {agent}>"
    return "|".join("" if v is None else str(v) for v in row)


def capture_specimen(
    agent: str,
    *,
    now: datetime,
    sac_bin: str = "sac",
    root: Path | None = None,
) -> Specimen:
    """Capture ``agent``'s full live state to a file. Returns where, and whether.

    Called BEFORE the restart, and its ``ok`` gates the restart. Never raises:
    every probe failure becomes labelled text inside the specimen, so a partial
    capture is still a capture and still says what it is missing.
    """
    pid = _pane_pid(agent)
    body = (
        f"=== captured {now.isoformat()} agent={agent} ===\n"
        f"--- auth-status (full) ---\n{_auth_status_table(sac_bin)}\n"
        f"--- pane pid ---\n{pid}\n{_ps_line(pid)}\n"
        f"--- pane capture (full scrollback tail {_SCROLLBACK_LINES}) ---\n"
        f"{_run(['tmux', 'capture-pane', '-t', exact_target(f'{_TUI_PREFIX}{agent}'), '-p', '-S', f'-{_SCROLLBACK_LINES}'])}\n"
        f"--- state.db row ---\n{_state_db_row(agent)}\n"
    )
    target = specimen_path(agent, now=now, root=root)
    # stx-allow: fallback (reason: a specimen that cannot reach disk must return
    # ok=False so the caller REFUSES to restart — the reproduction has to exist
    # before the evidence is destroyed, so this failure is load-bearing)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        return Specimen(
            path=target,
            ok=False,
            detail=(
                f"could NOT write the pre-restart specimen to {target} ({exc}). "
                f"Restarting now would destroy the only live reproduction of "
                f"this failure with nothing saved"
            ),
            text=body,
        )
    return Specimen(path=target, ok=True, detail=str(target), text=body)
