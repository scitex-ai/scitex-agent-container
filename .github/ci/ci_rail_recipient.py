"""WHO the verdict is for, and HOW SURE the rail is about that.

Companion to :mod:`ci_card_rail` (orchestration + CLI), :mod:`ci_rail_cards`
(the card contract), :mod:`ci_rail_listen` (delivery) and
:mod:`ci_rail_message` (the text). Like them, this module is a FAILURE
DOMAIN rather than a size bucket: "the verdict reached the wrong agent" is a
different bug, with different evidence, from "the store was wrong" or "the
bus was deaf", and until now it was the only one of the four with no home.

THE DEFECT THIS MODULE WAS CARVED OUT TO FIX. Addressing had two sources of
truth and one field to write them in. ``pre-push`` records a MEASURED
identity — the pushing process's own environment. When that record is
missing, the verdict half INFERRED one by matching agent specs against the
repository name. Both then went into ``agent``/``assignee``, so an inference
became indistinguishable from an observation the instant it was written; and
because the next run read that field back as "the recorded pusher", the
rail's own guess was promoted to its own evidence one run later.

Measured on the live store 2026-08-15: 118 of 121 rail-shaped cards (97.5%)
carried an inferred owner recorded as fact, 117 of them naming this repo's
owning agent. See ``UNCLAIMED_OWNER`` in :mod:`ci_rail_cards`.

So the answer this module returns is a PAIR, always, and the second half is
not commentary — it is the part that decides whether the first half may be
written down as authorship.

DEPENDENCIES: standard library, plus the two sentinels from
:mod:`ci_rail_cards`. No network and no store: every function here is a pure
decision over data the caller already fetched, which is what makes the
addressing rules testable without a postgres or a daemon.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_rail_cards import (  # noqa: E402 — sibling module, path fixed up above
    UNCLAIMED_OWNER,
    VERDICT_ACTOR,
    repo_basename,
)

# The identity of the pushing agent, in precedence order.
# ``$SCITEX_TODO_AGENT_ID`` is the card package's own canonical variable
# and so leads — but it is NOT reliably present: measured unset in this
# container's shell, and present in the cards MCP server process as the
# literal, unexpanded string ``${SCITEX_TODO_AGENT_ID}``. The sac-side
# names are set by the runtime that launched the container and were
# measured correct, so they are working fallbacks rather than decoration.
# An owner-less card is REJECTED by the store — there is no silent
# fallback there — so resolving this is what makes the push half work.
AGENT_ID_ENV_VARS = (
    "SCITEX_TODO_AGENT_ID",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_AGENT",
    "CLAUDE_AGENT_ID",
)

# HOW THE RECIPIENT WAS DECIDED — three values, because there are three
# facts. Returned by :func:`resolve_recipient` and consumed by
# ``record_verdict``, which writes a DIFFERENT card for each.
#
#   HOW_RECORDED  the pusher is KNOWN     -> record it as authorship
#   HOW_FALLBACK  the pusher is UNKNOWN   -> route to a guess, record NOTHING
#   HOW_NONE      nobody at all           -> fail loudly
#
# Named constants rather than bare strings so the branch in ``record_verdict``
# cannot silently stop matching after a rename — a string typo there would
# make every verdict take the "unknown" path, or worse, the "known" one.
#
# THE VALUES ARE THE ORIGINAL WIRE STRINGS, deliberately. They already
# appear in run logs and in the existing suite's assertions; renaming them
# would churn a neighbouring 739-line test file for no behavioural gain,
# and this change is about what gets WRITTEN TO THE CARD, not about what
# the provenance is spelled. The constant NAMES carry the meaning at every
# call site, which is where a reader decides whether a value may be
# recorded as authorship.
HOW_RECORDED = "card"
HOW_FALLBACK = "spec"
HOW_NONE = "unresolved"

# Names this rail writes ITSELF, which therefore prove nothing about who
# pushed. Reading either back as a recorded pusher is how an inference
# becomes a fact one run later. See :func:`resolve_recipient`.
NOT_A_PUSHER = (UNCLAIMED_OWNER, VERDICT_ACTOR)

__all__ = [
    "AGENT_ID_ENV_VARS",
    "HOW_FALLBACK",
    "HOW_NONE",
    "HOW_RECORDED",
    "NOT_A_PUSHER",
    "attribution",
    "pushing_agent",
    "resolve_recipient",
]


def attribution(*, recipient: str, how: str, repo: str) -> tuple[str, str]:
    """Turn ``(recipient, how)`` into ``(card_owner, routing_disclaimer)``.

    THE DECISION THE DEFECT GOT WRONG, ISOLATED SO IT CAN BE MEASURED. The
    old code had no such step: ``record_verdict`` wrote ``agent=recipient,
    assignee=recipient`` unconditionally, which is correct on one of the two
    paths and a fabricated fact on the other. Naming the choice makes the
    two paths visible, and makes them testable without a store, a bus or a
    runner -- the reason it is a pure function over three strings.

    ``card_owner`` is what goes in ``agent``/``assignee``. It is
    ``recipient`` ONLY when ``how`` says the pusher was recorded; otherwise
    it is :data:`~ci_rail_cards.UNCLAIMED_OWNER`, because the store refuses
    an owner-less card and a sentinel is the only remaining way to say "we
    do not know" in a field that has no empty state.

    ``routing_disclaimer`` is empty exactly when the honest answer to "why
    did this reach me?" is "because you pushed". Otherwise it is the
    sentence the recipient needs in order to not act on somebody else's
    verdict -- the half of the fix a card field cannot carry, since only the
    message reaches a person.
    """
    if how == HOW_RECORDED:
        return recipient, ""
    return UNCLAIMED_OWNER, (
        "ROUTING — THE PUSHING AGENT IS UNKNOWN for this commit. No pre-push "
        "record exists, so this reached you because your agent spec declares "
        f"project={repo_basename(repo)!r}, NOT because you pushed. The card "
        f"is filed under {UNCLAIMED_OWNER!r} and you are a subscriber on it, "
        "not its owner. If this push was not yours, this is not your verdict "
        "to act on."
    )


def pushing_agent() -> str | None:
    """First non-empty, non-template agent identity from the environment."""
    for var in AGENT_ID_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        # Reject an unexpanded shell template rather than filing cards
        # owned by an agent literally named "${SCITEX_TODO_AGENT_ID}".
        if value and not value.startswith("${"):
            return value
    return None


def resolve_recipient(
    *, card: dict[str, Any] | None, repo: str, agents: list[dict[str, Any]]
) -> tuple[str | None, str]:
    """Who should hear this verdict? Returns ``(name, how_it_was_decided)``.

    ``how`` IS PART OF THE ANSWER, NOT COMMENTARY ON IT. It is the only
    thing separating "the pusher is X" from "we picked X off the repo
    name", and the caller must not write the second into a field that
    means the first. Exactly three values, and the caller handles each
    differently:

    ``HOW_RECORDED`` — **the card's own agent**, set by ``pre-push`` from
        the pushing process's own environment. A MEASURED identity. A
        verdict belongs to the pusher, and no inference beats a record of
        the fact.
    ``HOW_FALLBACK`` — **an agent spec whose ``project`` matches the repo,
        PREFERRING one reachable right now.** This is an inference FROM THE
        REPOSITORY: it answers "who usually owns this repo", never "who
        pushed this commit". sac's ``_ci_owner.resolve_owner`` makes the
        same match but takes the first hit in sorted filename order, which
        on this very repo deterministically selects
        ``scitex-agent-container-04`` — zero inbox subscribers since
        2026-08-10. Sorting reachable candidates first is the difference
        between a delivered verdict and a silent one, and is why this does
        not simply call that function. It is still only a ROUTING choice;
        popularity is not authorship, and the caller records none of it.
    ``HOW_NONE`` — nobody at all. ``name`` is ``None`` and the caller must
        treat it as an error, never as a quiet skip.

    WHY THE FALLBACK IS NOT SIMPLY DELETED. It is load-bearing today: 118
    of 121 rail cards on the live store had no recorded pusher, because
    ``.githooks/pre-push`` only runs where ``core.hooksPath`` has been
    pointed at it (unset in this worktree, and absent by construction on
    every human and GitHub-side push). Removing the fallback would send
    ``record_verdict`` down its ``_die`` path for ~97% of pushes — turning
    the gate red and DESTROYING the verdict instead of delivering it. So
    the fallback still chooses a recipient; the fix is that the caller no
    longer launders that choice into ``agent``/``assignee``.

    A CARD OWNED BY THE UNCLAIMED SENTINEL IS NOT A RECORDED PUSHER. This
    rail writes ``UNCLAIMED_OWNER`` itself when the pusher is unknown, so
    reading it back as ``HOW_RECORDED`` would let one run's guess become
    the next run's "fact" — the exact laundering this function exists to
    stop — and would then address a verdict to a name that owns no inbox.
    The same applies to ``VERDICT_ACTOR``. Both are rejected here, and
    that rejection is not hypothetical: the same sha is judged twice
    whenever a branch has an open PR (the ``push`` and ``pull_request``
    events each fire this workflow), so the second run reads exactly what
    the first one wrote.
    """
    if card:
        named = card.get("agent") or card.get("assignee")
        if isinstance(named, str) and named.strip() not in ("", *NOT_A_PUSHER):
            return named.strip(), HOW_RECORDED

    base = repo_basename(repo)
    matches = [a for a in agents if str(a.get("project", "")).strip() == base]
    if not matches:
        return None, HOW_NONE
    matches.sort(
        key=lambda a: (
            a.get("inbox_reachable") != "reachable",
            -int(a.get("inbox_subscribers") or 0),
            str(a.get("started_at") or ""),
        )
    )
    name = matches[0].get("name")
    return (str(name) if name else None), HOW_FALLBACK


# EOF
