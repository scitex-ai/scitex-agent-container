#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare a RUNNING agent's argv against the flags its spec declares.

A spec file is INTENT. A process holds the argv it was launched with.
Nothing in the fleet compared the two, and that gap hid three separate
defects on 2026-08-19 alone:

* ``--effort low`` was added to 20 handyman specs and was present in
  exactly ONE running process. The other agents had started before the
  edit and were still running at default effort — the operator's cost
  instruction was satisfied on disk and not in the fleet.
* ``business``'s ``--env BUN_BIN=...`` was reverted in its spec and
  stayed intact in the running process, so nothing looked wrong; it
  would have surfaced only at the next restart, as a missing variable
  with no obvious cause.
* The cross-host spec guard, which compares spec against spec, is
  structurally blind to both.

All three share one shape: **the file changed and the process did not**,
and every surface that reads only files reports green.

WHY THIS COMPARES FLAGS AND NOT THE WHOLE ARGV
----------------------------------------------
Reconstructing the full launch argv and diffing it would be a stricter
check and a useless one: it carries values that legitimately differ per
launch (a2a port, session id, resume target, transcript home). Those
would dominate the output and every agent would report DIFFERS, which
is the fastest way to teach people to ignore a check.

``spec.claude.flags`` is the honest unit. Its contract is exact — each
element becomes ONE argv token, appended verbatim — so its presence in
the running argv is decidable without modelling anything else.

THREE VALUES, NEVER TWO
-----------------------
``CANNOT_DETERMINE`` is not a courtesy; it is the point. Tonight's
failures were all instruments whose "clean" and "could not look" printed
identically — a truncated listing read as absence, a spec-vs-spec guard
that cannot see a process, a status probe that says "real absence" about
another host. A drift check that answered only MATCHES/DIFFERS would
join them: an agent with no readable process would report MATCHES and
mean nothing by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

MATCHES = "matches"
DIFFERS = "differs"
CANNOT_DETERMINE = "cannot-determine"


@dataclass(frozen=True)
class ArgvDrift:
    """One agent's verdict.

    ``missing`` names the spec tokens absent from the running argv, so
    the report is actionable ("running without --effort low") rather
    than merely negative ("differs").
    """

    agent: str
    verdict: str
    reason: str | None = None
    missing: tuple[str, ...] = ()
    pid: int | None = None

    @property
    def is_drifted(self) -> bool | None:
        """``True``/``False``, or ``None`` when it could not be decided.

        Callers must handle the ``None``. A guard that treats it as
        falsey silently converts "I could not look" into "nothing
        wrong", which is the defect this module exists to expose.
        """
        if self.verdict == CANNOT_DETERMINE:
            return None
        return self.verdict == DIFFERS

    def describe(self) -> str:
        if self.verdict == CANNOT_DETERMINE:
            return f"{self.agent}: CANNOT DETERMINE — {self.reason}"
        if self.verdict == MATCHES:
            return f"{self.agent}: running argv carries every declared flag"
        return (
            f"{self.agent}: running without {' '.join(self.missing)}; "
            f"its spec declares it. The process predates the spec edit — "
            f"restart it to apply, or the spec is not in force."
        )


def _contiguous_index(haystack: Sequence[str], needle: Sequence[str]) -> int:
    """Index of ``needle`` as a contiguous run in ``haystack``, else -1."""
    n, m = len(haystack), len(needle)
    if m == 0:
        return 0
    for i in range(n - m + 1):
        if list(haystack[i : i + m]) == list(needle):
            return i
    return -1


def compare_spec_flags_to_argv(
    *,
    agent: str,
    spec_flags: Sequence[str] | None,
    running_argv: Sequence[str] | None,
    pid: int | None = None,
) -> ArgvDrift:
    """Decide whether a running process carries the flags its spec declares.

    ``spec_flags`` is ``None`` when the spec could not be loaded, and
    ``running_argv`` is ``None`` when no process was observed. Both are
    CANNOT_DETERMINE rather than a verdict — an unreadable input cannot
    exonerate anything.

    Adjacency is checked, not just membership. ``--effort`` and ``low``
    must appear as a contiguous run: a flag separated from its value is
    a different command line, and the inverse error (gluing them into
    one element, ``--effort ultracode``) left an agent unbootable for 15
    days without the YAML ever failing to parse.
    """
    if spec_flags is None:
        return ArgvDrift(
            agent=agent,
            verdict=CANNOT_DETERMINE,
            reason="the spec could not be loaded, so there is nothing to compare against",
            pid=pid,
        )
    if running_argv is None:
        return ArgvDrift(
            agent=agent,
            verdict=CANNOT_DETERMINE,
            reason="no running process was observed for this agent",
            pid=pid,
        )
    if not spec_flags:
        return ArgvDrift(agent=agent, verdict=MATCHES, pid=pid)

    argv = list(running_argv)
    missing = tuple(tok for tok in spec_flags if tok not in argv)
    if missing:
        return ArgvDrift(
            agent=agent, verdict=DIFFERS, missing=missing, pid=pid
        )

    if _contiguous_index(argv, list(spec_flags)) < 0:
        return ArgvDrift(
            agent=agent,
            verdict=DIFFERS,
            missing=(),
            reason=(
                "every declared flag is present but not as one contiguous run; "
                "a flag separated from its value is a different command line"
            ),
            pid=pid,
        )

    return ArgvDrift(agent=agent, verdict=MATCHES, pid=pid)


def summarize(drifts: Sequence[ArgvDrift]) -> str:
    """One line per agent plus a tally that keeps the three states apart.

    The tally never folds CANNOT_DETERMINE into either column. A run of
    30 agents where 29 could not be read is not "1 drifted"; it is one
    finding and 29 unanswered questions, and the summary has to say so.
    """
    drifted = [d for d in drifts if d.verdict == DIFFERS]
    unknown = [d for d in drifts if d.verdict == CANNOT_DETERMINE]
    ok = [d for d in drifts if d.verdict == MATCHES]
    lines = [d.describe() for d in drifts]
    lines.append(
        f"{len(ok)} in force, {len(drifted)} drifted, {len(unknown)} could not be determined"
    )
    return "\n".join(lines)


__all__ = [
    "ArgvDrift",
    "CANNOT_DETERMINE",
    "DIFFERS",
    "MATCHES",
    "compare_spec_flags_to_argv",
    "summarize",
]
