"""Apply the deployment-side rules to the executable's verdict.

The executable decides WHETHER to block and WHAT to say. This module owns
only what sac owns:

* **fail-open** on an unreadable executable,
* **the loop guard**, which needs to know how many times THIS SESSION has
  been blocked — session state, which the executable has no view of. That
  is why the guard lives here and not with scitex-cards.

There is deliberately no continuation composer any more. The ``reason`` that
becomes the agent's next instruction is authored by the executable and
passed through untouched; an earlier draft rendered it here from parsed
fields, which meant sac had to know their output format.

Decision table::

    ALLOW    → allow the stop, clear the loop-guard counter
    BLOCK    → forward their decision verbatim
               ...unless the loop guard tripped, then ALARM + allow
    UNKNOWN  → allow the stop, LOUDLY (fail open — a broken executable must
               never wedge an agent in an unstoppable loop)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._detector import BLOCK, UNKNOWN, Verdict
from ._loop_guard import MAX_CONSECUTIVE_BLOCKS, clear_blocks, record_block


@dataclass(frozen=True)
class HookDecision:
    """What the Stop hook should emit.

    ``payload`` is the hook JSON to write to stdout. ``log`` goes to stderr
    for the debug log; ``system_message`` surfaces to the operator.
    """

    block: bool
    payload: dict = field(default_factory=dict)
    log: str = ""
    system_message: str = ""


def decide(agent: str, verdict: Verdict) -> HookDecision:
    """Map ``verdict`` to the hook's decision, applying the loop guard."""
    if verdict.state == UNKNOWN:
        # Fail OPEN, loudly. "We could not tell" must never be silently
        # served as "nothing to do" — but it must also never wedge the agent.
        msg = (
            f"never-stop-when-task-remains: allowing the stop because the "
            f"runnable-work check could not be read ({verdict.detail}). This "
            f"is FAIL-OPEN: work may still be pending on "
            f"{agent or 'this agent'}'s board and nothing checked it."
        )
        return HookDecision(block=False, log=msg, system_message=msg)

    if verdict.state != BLOCK:
        clear_blocks(agent)
        return HookDecision(block=False)

    count, tripped = record_block(agent, verdict.block_signature_source())

    if tripped:
        msg = (
            f"never-stop-when-task-remains: ALARM — blocked "
            f"{MAX_CONSECUTIVE_BLOCKS} consecutive stops for {agent} with an "
            f"unchanged block reason, i.e. no observable progress. Allowing "
            f"the stop instead of re-driving again: an agent that can never "
            f"end its turn is a worse failure than an idle one. This work is "
            f"stuck and needs a human or a different owner."
        )
        return HookDecision(block=False, log=msg, system_message=msg)

    log = ""
    if count > 1:
        log = (
            f"never-stop-when-task-remains: block {count}/"
            f"{MAX_CONSECUTIVE_BLOCKS} on an unchanged block reason for {agent}"
        )

    # Forward their decision verbatim. If they emitted hook JSON we pass that
    # object through; otherwise we wrap their opaque stderr as the reason.
    payload = (
        dict(verdict.payload)
        if verdict.payload is not None
        else {"decision": "block", "reason": verdict.reason}
    )
    return HookDecision(block=True, payload=payload, log=log)


__all__ = ["HookDecision", "decide"]
