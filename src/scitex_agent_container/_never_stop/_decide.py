"""Turn a detector verdict into the Stop hook's decision.

The important part of this module is that a block is not a refusal. Claude
Code's Stop hook feeds ``reason`` back as the agent's next instruction, so
``reason`` is where "you may not stop" becomes "here is the next item, take
it". A bare refusal would leave the agent exactly where the incident found
it: awake, unblocked, and still idle.

Decision table::

    ALLOW     → allow the stop, clear the loop-guard counter
    RUNNABLE  → block, hand over the parsed next_action list
                ...unless the loop guard tripped, then ALARM + allow
    UNKNOWN   → allow the stop, LOUDLY (fail open — a broken detector must
                never wedge an agent in an unstoppable loop)
"""

from __future__ import annotations

from dataclasses import dataclass

from ._detector import RUNNABLE, UNKNOWN, Verdict
from ._loop_guard import MAX_CONSECUTIVE_BLOCKS, clear_blocks, record_block

#: Cap on items rendered into the continuation prompt — a 40-card board must
#: not produce a 40-item wall the agent skims instead of acts on.
_ITEM_CAP = 10


@dataclass(frozen=True)
class HookDecision:
    """What the Stop hook should do.

    ``reason`` is the continuation prompt (only meaningful when ``block``).
    ``log`` goes to stderr for the debug log; ``system_message`` surfaces to
    the operator via the hook's ``systemMessage`` field.
    """

    block: bool
    reason: str = ""
    log: str = ""
    system_message: str = ""


def _render_items(verdict: Verdict) -> str:
    lines = []
    for idx, item in enumerate(verdict.items[:_ITEM_CAP], start=1):
        head = f"{idx}. {item.card_id}"
        if item.reason:
            head += f" — {item.reason}"
        lines.append(head)
        if item.next_action:
            lines.append(f"   NEXT ACTION: {item.next_action}")
    extra = len(verdict.items) - _ITEM_CAP
    if extra > 0:
        lines.append(f"   (+{extra} more runnable item(s) on your board)")
    return "\n".join(lines)


def _continuation(agent: str, verdict: Verdict) -> str:
    """Compose the next-item hand-off that replaces the stop."""
    count = len(verdict.items)
    if not count:
        # Exit 2 with unparseable detail: work exists, we just cannot name it.
        return (
            f"Do NOT stop. Your board ({agent}) still holds runnable work — the "
            "detector confirmed it, but its item list could not be parsed. Run "
            f"`scitex-cards runnable --agent {agent}` (or `scitex-cards next "
            f"--agent {agent}`) to see what is pending, and take the top item "
            "now. If nothing is genuinely actionable, give each open card an "
            "honest disposition (finish / reassign / set blocked with a named "
            "blocker / defer) before stopping."
        )

    idle = ""
    if verdict.idle_seconds is not None and verdict.idle_seconds > 0:
        idle = f" You have been idle for ~{verdict.idle_seconds}s."

    return (
        f"Do NOT stop — you ({agent}) have {count} runnable item(s) on your "
        f"board.{idle} Finishing one unit of work is the trigger to take the "
        "next, so take item 1 NOW rather than ending the turn:\n\n"
        f"{_render_items(verdict)}\n\n"
        "Start executing item 1 immediately. If an item is genuinely not "
        "actionable, give it an honest disposition on the board (finish it, "
        "reassign it, set it blocked with a named blocker, or defer it) — that "
        "removes it from the runnable set and is the only legitimate way to "
        "reach a stop."
    )


def decide(agent: str, verdict: Verdict) -> HookDecision:
    """Map ``verdict`` to the hook's decision, applying the loop guard."""
    if verdict.state == UNKNOWN:
        # Fail OPEN, loudly. "We could not tell" must never be silently
        # served as "nothing to do" — but it must also never wedge the agent.
        msg = (
            f"never-stop: allowing the stop because the runnable-work detector "
            f"could not be read ({verdict.detail}). This is FAIL-OPEN: work may "
            f"still be pending on {agent or 'this agent'}'s board and nothing "
            f"checked it."
        )
        return HookDecision(block=False, log=msg, system_message=msg)

    if verdict.state != RUNNABLE:
        clear_blocks(agent)
        return HookDecision(block=False)

    count, tripped = record_block(agent, verdict.card_ids)

    if tripped:
        named = ", ".join(verdict.card_ids[:_ITEM_CAP]) or "(unnamed items)"
        msg = (
            f"never-stop: ALARM — blocked {MAX_CONSECUTIVE_BLOCKS} consecutive "
            f"stops for {agent} with no observable progress on the same "
            f"runnable set ({named}). Allowing the stop instead of re-driving "
            f"again: an agent that can never end its turn is a worse failure "
            f"than an idle one. These cards are stuck and need a human or a "
            f"different owner."
        )
        return HookDecision(block=False, log=msg, system_message=msg)

    log = verdict.detail or ""
    if count > 1:
        log = (
            f"never-stop: block {count}/{MAX_CONSECUTIVE_BLOCKS} on an unchanged "
            f"runnable set for {agent}" + (f" ({log})" if log else "")
        )
    return HookDecision(block=True, reason=_continuation(agent, verdict), log=log)


__all__ = ["HookDecision", "decide"]
