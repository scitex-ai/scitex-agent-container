"""Never stop when a task remains — register the Stop hook, don't reimplement it.

THE INVARIANT
-------------
While an agent's board holds runnable work, that agent is EXECUTING work.
"Idle with work pending" must be UNREACHABLE BY DESIGN — not a state we
detect after the fact and repair.

INCIDENT (operator, 2026-07-18): scitex-hub sat idle at its prompt for 80+
minutes holding 5 ``in_progress`` cards, and the OPERATOR noticed it twice.
A notification was sent and changed nothing — a stopped agent reads nothing.
Recovery-after-the-fact is what this package replaces.

THE MECHANISM
-------------
The completion of one unit of work IS the trigger to take the next one. The
agent PULLS its next item; nobody pushes it. That is enforced by a Claude
Code **Stop hook**: when the agent tries to end its turn while runnable work
remains, the stop is BLOCKED and CONVERTED into taking the next item.

Merely refusing the stop is not sufficient — a refused stop leaves the agent
sitting there idle — so the block carries a ``reason`` that becomes the
agent's next instruction.

OWNERSHIP BOUNDARY (agreed with scitex-cards)
---------------------------------------------
**scitex-cards ships the hook executable.** It decides whether work remains
and authors the ``reason``. It owns both ends of that contract, so its
output format is not an API it cannot change.

**sac registers it and owns the deployment mechanics**, which is everything
in this package:

* :mod:`._identity` — resolve WHICH agent we are, from the environment only.
* :mod:`._detector` — run the executable and classify the RESULT. Passes
  their hook JSON through verbatim; never inspects their domain fields.
* :mod:`._loop_guard` — count consecutive identical blocks. This needs
  SESSION state (how many times this session was blocked), which the
  executable has no view of, which is why it is ours.
* :mod:`._decide` — fail-open and loop-guard rules over their verdict.
* :mod:`._awaiting_operator` — REPORT the cards blocked on a human. Read
  path only; it never gates and never mutates. See its docstring for why a
  blocked card had stopped existing, and why the line rides on
  ``systemMessage`` rather than on the block ``reason``.

An earlier draft of this package parsed scitex-cards' stdout JSON fields and
its numbered stderr hint lines. That made their output an API they could not
change without breaking us — the exact coupling that was removed in the
other direction when cards' bridge was killed for depending on sac. The
parsing layer is gone.

THREE STATES, NEVER TWO
-----------------------
``allow`` / ``block`` / ``unknown``. "Nothing is runnable" and "we could not
tell" are different facts and must not collapse into the same pole. Only
``unknown`` fails open — loudly, because a silent allow is indistinguishable
from a clean board, which is where the incident hid.

NON-GOAL — this is not a poller
-------------------------------
There is deliberately NO scheduled watchdog/sweep here. A sweep is only ever
a FAILURE NET (for a dead process, a wedged session, or a hook that never
fired) — never the mechanism. Do not "improve" this package into a poller.
"""

from __future__ import annotations

from ._awaiting_operator import OPERATOR_BLOCKER, notice
from ._decide import HookDecision, decide
from ._detector import ALLOW, BLOCK, UNKNOWN, Verdict, detector_argv, probe
from ._identity import IDENTITY_ENV_VARS, resolve_agent
from ._loop_guard import MAX_CONSECUTIVE_BLOCKS, clear_blocks, record_block

__all__ = [
    "ALLOW",
    "BLOCK",
    "IDENTITY_ENV_VARS",
    "MAX_CONSECUTIVE_BLOCKS",
    "OPERATOR_BLOCKER",
    "UNKNOWN",
    "HookDecision",
    "Verdict",
    "clear_blocks",
    "decide",
    "detector_argv",
    "notice",
    "probe",
    "record_block",
    "resolve_agent",
]
