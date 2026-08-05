"""One agent's line in an auth-heal pass, and the NON-RESTART outcomes.

Extracted from :mod:`._pass` so that file keeps headroom under the 512-line
cap. Pure extraction — no behaviour change. :mod:`._pass` re-exports every
name here, so ``from .._authheal._pass import AgentReport`` keeps resolving.

What belongs here: the record itself, plus the constructors for outcomes where
the pass took NO action. The restart-attempt reports stay next to the code that
attempts the restart — a report that describes an action should not drift away
from the action it describes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._reconcile._rule import Verdict

#: The report subject used when the ROSTER ITSELF could not be read. Not an
#: agent — it is the population we failed to establish — and named so that a
#: cron log says WHICH reading failed instead of showing an unexplained exit 2.
_ROSTER_SUBJECT = "<fleet-roster>"


@dataclass(frozen=True)
class AgentReport:
    """One agent's line in the report. ``detail`` is ALWAYS printed."""

    name: str
    verdict: Verdict
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
        }


def _unobserved(name: str, *, live: bool) -> AgentReport:
    """An agent we took NO reading of — reported, never restarted.

    There are two ways to be unobserved and the detail must say which, because
    they send the operator to different places: a LIVE session whose pane would
    not capture (tmux is there, the read failed), versus a registered agent with
    no session at all (the reading could not even have had a row for it — the
    shape that let an agent go missing rather than red). Neither is evidence of
    a wedge, so neither is restarted; neither is evidence of health either, so
    neither is silently dropped.

    The ``reason`` strings are load-bearing, not decoration:
    :meth:`PassOutcome.indeterminate` reads them to tell "we failed to look"
    from "there was nothing to look at". Renaming one changes an exit code.
    """
    if live:
        return AgentReport(
            name,
            Verdict.UNOBSERVED,
            "pane-unreadable",
            f"{name} has a live tui- session but its pane could NOT be captured, "
            f"so nothing was learned about its auth. NOT restarted (absence of "
            f"evidence is not evidence of a wedge) and NOT counted healthy",
        )
    return AgentReport(
        name,
        Verdict.UNOBSERVED,
        "no-session",
        f"{name} is REGISTERED but has no live tui- session, so this pass could "
        f"not read it at all. NOT restarted — a missing session is "
        f"fleet-reconcile's half of the fleet, not ours — but its absence from "
        f"the reading is not evidence that {name} is healthy",
    )


def _roster_unreadable(detail: str) -> AgentReport:
    """The ROSTER is the thing we could not read, so no pass can be clean.

    Without it we do not know which agents SHOULD have been observed, so we
    cannot claim to have observed them all however many panes we did read. The
    finding is carried as a report of its own rather than a bare exit code, so
    the reason travels with the verdict to whoever reads the log.
    """
    return AgentReport(
        _ROSTER_SUBJECT,
        Verdict.UNOBSERVED,
        "roster-unreadable",
        f"could not establish which agents SHOULD be running: {detail}. Agents "
        f"missing from this pass's reading cannot be told apart from agents that "
        f"do not exist, so this pass cannot report a clean fleet",
    )


__all__ = [
    "AgentReport",
    "_ROSTER_SUBJECT",
    "_roster_unreadable",
    "_unobserved",
]
