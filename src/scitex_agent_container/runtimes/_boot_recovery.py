"""On restart, make the agent go and READ what it missed.

OPERATOR DIRECTIVE 2026-08-03: 「リスタートした時に取りこぼしのある指示を
自分で読ませるべき」 — when an agent is restarted, it should read for itself
the instructions that were dropped.

THE CONCRETE LOSS THAT PROMPTED IT. scitex-hub's telegram MCP server was dead,
so operator messages were not reaching it. I restarted hub to fix that. The
operator had sent a message eight minutes BEFORE the restart; it died with the
old session. The rail was repaired and the instruction was still lost, and the
only reason anyone noticed is that the operator asked a third time.

Restarting is not neutral. It is the one moment when an agent is guaranteed to
have a gap, because the thing being replaced is the thing that was holding the
queue. Every restart therefore owes a read-back, and the agent is the only
party positioned to do it — the sender does not know a restart happened, and
the operator should never be the retry mechanism.

WHY THIS LIVES IN SAC AND NOT IN THE SPECS. The fleet's boot kick is
duplicated across 83 ``spec.startup_prompts`` entries, 81 of them byte
identical. Adding a line there means 83 edits that immediately begin to drift,
and a new agent created tomorrow inherits whichever copy it was cloned from.
Injected here it is a fleet invariant: one definition, applies to every agent
including ones that do not exist yet, and cannot be half-applied.

DELIBERATELY PHRASED AS "check and act", NOT "you have missed messages". At
injection time sac does not know whether anything was actually missed, and a
prompt that asserts a backlog that may be empty trains the reader to skip it.
"""

from __future__ import annotations

#: Injected as the FIRST turn of every restarted session, ahead of the spec's
#: own prompts. Short on purpose: it costs a few tokens on every boot of every
#: agent, so it earns its place by being one instruction, not a briefing.
MISSED_INPUT_RECOVERY_PROMPT = (
    "You have just (re)started, so anything sent to you while you were down is "
    "NOT in this session. Before doing anything else, go and read it: check "
    "your operator channel for unread/recent messages (e.g. the telegrammer "
    "get_unread / get_history tools) and poll your card notifications. Act on "
    "anything still outstanding, and if something was addressed to you that you "
    "can no longer recover, say so plainly rather than staying silent. Do NOT "
    "assume a quiet inbox means nothing was sent -- a restart drops in-flight "
    "messages, and the sender does not know you restarted."
)


def with_missed_input_recovery(prompts) -> list[str]:
    """The spec's startup prompts, preceded by the read-back instruction.

    Pure, and total over its input — this is the seam the tests drive.

    Returns the recovery prompt even when the spec declares NO startup prompts.
    That is the point rather than an oversight: an agent with no boot kick is
    not an agent with nothing to catch up on, and it is the one least likely to
    look on its own.
    """
    tail = [p for p in list(prompts or []) if p]
    return [MISSED_INPUT_RECOVERY_PROMPT, *tail]
