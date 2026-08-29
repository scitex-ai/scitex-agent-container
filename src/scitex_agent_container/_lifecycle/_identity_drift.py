"""Launch-time check: does this agent's BOARD identity match its NAME?

An agent has two names that must agree and are set in different places:

* the agent NAME — its spec dir, its tmux session, how peers address it
  over a2a;
* ``SCITEX_CARDS_AGENT_ID`` — its identity on the shared card board, which
  determines which cards it owns and whose inbox it polls. It was called
  ``SCITEX_TODO_AGENT_ID`` until 2026-07-02 and specs still carry both
  spellings, so this check reads BOTH.

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
NOT a symptom, though it was recorded as one at the time: card writes come
back with ``assignee_liveness: unknown``. That is true for EVERY agent,
drifted or not -- verified 2026-08-25 against the live board by an agent
whose identity was correct and which was demonstrably running. A field that
reads the same in both states cannot discriminate between them, so it is
evidence of nothing and must not be used to diagnose this.

Nothing errored. Both queries succeeded and returned well-formed empty
results, because "no cards assigned to you" and "no cards assigned to
THIS SPELLING of you" render identically.

_RETIRED_BLINDNESS
------------------
This check reads the spec's declared board identity under BOTH the current
and the retired env name. Reading one spelling only is the same defect the
module exists to catch: on 2026-08-25 the check looked exclusively for the
RETIRED name, so for 110 of 148 specs on compute-04 it read an empty string,
concluded "this spec declares no board identity", and returned quietly. That
skip is a documented, legitimate branch -- which is what made the blindness
invisible.

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
#: This is the CURRENT name; `scitex_cards._store` reads exactly this
#: (``ENV_AGENT = "SCITEX_CARDS_AGENT_ID"``).
BOARD_IDENTITY_ENV = "SCITEX_CARDS_AGENT_ID"

#: The RETIRED spelling, renamed 2026-07-02. Specs are migrated one at a time,
#: so both are live on disk at once -- measured on compute-04 2026-08-25:
#: 110 specs declared the current name, 21 still declared this one.
#: Reading only ONE of them is precisely the fault this module exists to catch,
#: so the check reads BOTH. See `_RETIRED_BLINDNESS` in the module docstring.
BOARD_IDENTITY_ENV_RETIRED = "SCITEX_TODO_AGENT_ID"


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
        board_id = ""
        declared_under = BOARD_IDENTITY_ENV
        for _key in (BOARD_IDENTITY_ENV, BOARD_IDENTITY_ENV_RETIRED):
            board_id = str(spec_env.get(_key, "") or "").strip()
            if board_id:
                declared_under = _key
                break
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
        declared_under,
        board_id,
        name,
        board_id,
        name,
        board_id,
    )
    return board_id


__all__ = [
    "BOARD_IDENTITY_ENV",
    "BOARD_IDENTITY_ENV_RETIRED",
    "check_board_identity_at_launch",
]
