"""Emit a captured pane as CONTEXT at INFO, apart from the fault that names it.

A pane tail is a transcription of ANOTHER session's screen. Carried inside the
error record that reports a boot fault it inherits that record's level, and the
log formatter stamps the level onto EVERY line — so a start that succeeded
scrolls past as::

    sac-start ERRO: TuiSessionRuntime: stale compose buffer ... Pane tail:
    ERRO: ✻ Running scheduled task (Jul 23 1:25am)
    ERRO: ❯ /compact
    SUCC: grant started

Fourteen lines of someone else's UI, all labelled as failures, with the one
line that IS the fault indistinguishable among them. Splitting the
transcription into its own INFO record keeps the fault loud and its evidence
quiet; the final SUCC/ERROR line stays the outcome.

The fault message itself is unchanged and stays at whatever level it earned —
"the compose buffer did not clear after 8 attempts" is a real condition and
belongs at error.
"""

from __future__ import annotations

import logging

#: Rendered when the caller has no pane to show. A pane tail that is empty
#: because the session was already gone is evidence too, and silently logging
#: a blank record would read as "nothing was wrong here".
NO_PANE_CAPTURED = "(nothing captured)"

_PANE_CONTEXT_TEMPLATE = (
    "TuiSessionRuntime: pane tail for %s — a copy of that session's screen, "
    "logged as context for the message above (not itself a fault):\n%s"
)


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


__all__ = [
    "NO_PANE_CAPTURED",
    "log_pane_context",
    "pane_tail",
]
