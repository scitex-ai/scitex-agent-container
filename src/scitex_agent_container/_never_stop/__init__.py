"""Never-stop actuator — turn a stop-with-work-remaining into taking the next item.

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

Merely refusing the stop is NOT sufficient — a refused stop leaves the agent
sitting there idle. The hook must HAND IT THE NEXT ACTION, which is why
:func:`~._decide.decide` composes the parsed ``next_action`` list into the
continuation prompt rather than emitting a bare "don't stop".

THE DETECTOR (owned elsewhere — we do not reimplement it)
---------------------------------------------------------
``scitex-cards may-stop --agent <id>``, owned by scitex-cards. Contract:

* **exit 0** — nothing runnable. Stopping is allowed. Definite.
* **exit 2** — runnable work exists. STDOUT is one-line JSON
  ``{"agent", "runnable": true, "items": [{"card_id", "reason",
  "next_action"}, ...], "idle_seconds": <int>}``. STDERR carries numbered
  hint lines ``N. <card_id> — <reason> — <next_action>``.

STDERR may ALSO carry tolerated store read-warnings ABOVE the hints, so
hints are parsed by the NUMBERED-LINE PATTERN, never by position. This is
not hypothetical: the deployed store emits several ``[scitex-todo]
TOLERATED (read-side)`` lines plus a ``SCITEX_TODO_*`` deprecation warning
before any payload.

THREE STATES, NEVER TWO
-----------------------
A detector verdict is ``allow`` / ``runnable`` / ``unknown`` — never a
boolean. "The detector said nothing is runnable" and "we could not tell"
are different facts and must not collapse into the same pole. An exit 2 we
merely failed to PARSE still means work exists, so it blocks; only a
genuinely unreadable detector (missing, timed out, crashed, unexpected
rc) yields ``unknown``.

FAIL-OPEN
---------
``unknown`` ALLOWS the stop and logs loudly. A broken detector must never
wedge an agent in an unstoppable loop. Today this is the LIVE path, not a
hypothetical one: the deployed scitex-cards (v0.16.1) has no ``may-stop``
subcommand yet, so the hook currently fails open on every turn until that
ships.

NON-GOAL — this is not a poller
-------------------------------
There is deliberately NO scheduled watchdog/sweep here. scitex-cards ships
a bridge timer separately, and long-term a sweep is only a FAILURE NET (for
a dead process, a wedged session, or a hook that never fired) — never the
mechanism. Do not "improve" this package into a poller.
"""

from __future__ import annotations

from ._decide import HookDecision, decide
from ._detector import RunnableItem, Verdict, detector_argv, probe
from ._identity import IDENTITY_ENV_VARS, resolve_agent
from ._loop_guard import MAX_CONSECUTIVE_BLOCKS, clear_blocks, record_block

__all__ = [
    "IDENTITY_ENV_VARS",
    "MAX_CONSECUTIVE_BLOCKS",
    "HookDecision",
    "RunnableItem",
    "Verdict",
    "clear_blocks",
    "decide",
    "detector_argv",
    "probe",
    "record_block",
    "resolve_agent",
]
