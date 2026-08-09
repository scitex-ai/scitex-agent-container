"""Launch-time check: does this agent's BOARD identity match its NAME?

An agent has two names that must agree and are set in different places:

* the agent NAME — its spec dir, its tmux session, how peers address it
  over a2a;
* ``SCITEX_TODO_AGENT_ID`` — its identity on the shared card board, which
  determines which cards it owns and whose inbox it polls.

``_create_templates`` derives the second from the first, so a
properly-created agent cannot drift. A HAND-EDITED spec can, and does.

INCIDENT 2026-08-09
-------------------
The sac maintainer on scitex-compute-04 ran with name
``scitex-agent-container-04`` and ``SCITEX_TODO_AGENT_ID`` of
``scitex-agent-container`` — one process, two identities, because the
spec was hand-made during a host migration. The consequences were all
SILENT:

* its routine sweep, ``list_tasks(assignee=<board identity>)``, returned
  ``[]`` and it twice reported "board is clear, holding idle" while a P1
  with a full implementation brief sat runnable under the OTHER name;
* its pull-inbox under the agent name accumulated unseen notifications
  it never polled, including a card another agent filed for it;
* every ``reassign_task`` it performed came back
  ``assignee_liveness: unknown``, so a real handoff was indistinguishable
  from a handoff into the void.

Nothing errored. Both queries succeeded and returned well-formed empty
results, because "no cards assigned to you" and "no cards assigned to
THIS SPELLING of you" render identically.

Contract
--------
LOUD WARNING, never a block — deliberately the same contract as the
sibling :func:`._start_preflight._check_spec_source_drift_at_launch`. A
mismatch is usually a migration artifact on an agent that is otherwise
working, and refusing to launch would strand it rather than inform its
operator. Best-effort throughout: this check never crashes a launch.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The env var whose value IS the agent's identity on the card board.
BOARD_IDENTITY_ENV = "SCITEX_TODO_AGENT_ID"


def check_board_identity_at_launch(config) -> str | None:
    """Warn when the spec's board identity differs from the agent name.

    Returns the mismatching board identity (for tests and callers that
    want to report it), or ``None`` when they agree, when the spec sets
    no board identity at all, or when anything is unreadable.

    A spec that sets NO board identity is not a mismatch: the agent
    inherits whatever the runtime provides, which is the documented
    default path and not the failure this guards.
    """
    # stx-allow: fallback (reason: a launch-time advisory must never crash
    # the launch; an unreadable spec here simply yields no warning, and the
    # spec loader itself reports malformed specs far more precisely.)
    try:
        name = str(getattr(config, "name", "") or "")
        spec_env = getattr(config, "env", None) or {}
        board_id = str(spec_env.get(BOARD_IDENTITY_ENV, "") or "").strip()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None

    if not name or not board_id or board_id == name:
        return None

    logger.warning(
        "IDENTITY MISMATCH for agent %r: its %s is %r. This agent is ONE "
        "process with TWO names — peers address it as %r over a2a, while it "
        "owns cards and polls its inbox as %r. THE AGENT STARTS NORMALLY; "
        "this is not a startup failure. What it costs: a card sweep by one "
        "name returns an empty list while work sits runnable under the "
        "other, and neither query errors, because 'no cards assigned to you' "
        "and 'no cards assigned to THIS SPELLING of you' look identical. "
        "Measured 2026-08-09: exactly this hid a P1 from the agent that "
        "owned it for over an hour. Fix with `sac agents rename %s %s` "
        "(atomic across the six on-disk locations AND the board — it "
        "migrates the cards, which is the part that must not be done by "
        "hand; run it with --dry-run first).",
        name,
        BOARD_IDENTITY_ENV,
        board_id,
        name,
        board_id,
        name,
        board_id,
    )
    return board_id


__all__ = ["BOARD_IDENTITY_ENV", "check_board_identity_at_launch"]
