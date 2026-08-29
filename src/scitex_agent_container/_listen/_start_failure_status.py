#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Map a failed ``sac agents start`` child onto an HTTP status that means something.

``POST /agents`` answered **502 for every non-zero child exit**, whatever
the cause. hub hit it standing up a scholar agent on 2026-08-19 and put
the problem better than the code did:

    a 500 cannot distinguish "your request was wrong" from "the server
    is broken", and those need OPPOSITE responses from the caller.

They could not tell whether to fix their call or wait for the daemon
owner, so they waited, and reported a permissions bug that did not
exist. The body carried the real reason the whole time; the status code
threw it away.

WHY THE DEFAULT STAYS 502
-------------------------
Every unmatched failure keeps today's behaviour. A misclassification is
harmful in both directions and this picks the direction that is merely
unhelpful rather than actively misleading:

* a server fault reported as 4xx tells the caller to fix a call that is
  already correct, and they stop retrying something that would have
  recovered
* a caller fault reported as 502 leaves them waiting — bad, but it is
  the status quo and it does not send them to fix the wrong thing

So a pattern earns a 4xx only when it is unambiguous. Silence maps to
502, not to a guess.

MATCH ON MARKERS, NOT ON SENTENCES
----------------------------------
The patterns below are deliberately short and structural. Matching a
whole human-readable sentence would make this classifier break the next
time somebody improves the wording — the message is written for a
person, and a person's message is not a stable interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SERVER_FAULT = "server-fault"
UNREGISTERED = "unregistered-agent"
STALE_SPEC = "stale-spec"
DECLINED = "declined"


@dataclass(frozen=True)
class StartFailure:
    """What kind of failure this was, and what the caller should do."""

    status: int
    kind: str
    hint: str

    @property
    def is_caller_fixable(self) -> bool:
        """``True`` when the caller can act; ``False`` when they must wait."""
        return 400 <= self.status < 500


# An unresolvable / unloadable spec. Both spellings are matched: the
# resolver's own FileNotFoundError, and the refusal added when the lead
# credential fallback was removed.
_UNREGISTERED = re.compile(
    r"(spec could not be loaded|Agent '[^']*' not found|no such agent)",
    re.IGNORECASE,
)

# The drift guard refusing a spec source that is behind its remote. The
# agent exists and the request is well-formed; the HOST needs a pull, so
# this is a conflict rather than a bad request or a server fault.
_STALE_SPEC = re.compile(r"(sac-drift|commit\(s\) BEHIND|STALE)", re.IGNORECASE)


def classify_start_failure(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    declined: bool = False,
) -> StartFailure:
    """Classify a failed start into an HTTP status with a reason and a hint.

    ``declined`` comes from the existing
    :func:`_lifecycle._start_decline.start_was_declined`, which already
    recognises an agent that refused its own start. It is passed in
    rather than re-derived so the two can never disagree.
    """
    blob = f"{stdout}\n{stderr}"

    # A DECLINE KEEPS 502 — deliberately, and it returns early so no
    # pattern below can reclassify it.
    #
    # It is tempting to call this a client error: it is not a crash, and
    # 409 reads well. But the caller cannot fix it by changing their
    # request — the AGENT refused its own start — so a 4xx would tell
    # them to correct a call that was already correct, which is the
    # precise harm this module exists to avoid. It is also an existing
    # wire contract with its own named test
    # (test_a_declined_brokered_start_returns_502), and narrowing the
    # opaque 502 does not license redefining the cases that already have
    # a decided meaning.
    if declined:
        return StartFailure(
            status=502,
            kind=DECLINED,
            hint=(
                "the agent declined its own start; read its stdout for the "
                "reason rather than retrying"
            ),
        )

    if _UNREGISTERED.search(blob):
        return StartFailure(
            status=404,
            kind=UNREGISTERED,
            hint=(
                "no spec for this agent on the target host — check the name, "
                "or deliver the spec there first. This is not a server fault "
                "and retrying unchanged will not help"
            ),
        )

    if _STALE_SPEC.search(blob):
        return StartFailure(
            status=409,
            kind=STALE_SPEC,
            hint=(
                "the target host's spec source is behind its remote; pull the "
                "spec repo on that host, then retry"
            ),
        )

    return StartFailure(
        status=502,
        kind=SERVER_FAULT,
        hint=(
            f"the start subprocess exited {returncode} for a reason this "
            "daemon does not recognise; read stderr in the body"
        ),
    )


__all__ = [
    "DECLINED",
    "SERVER_FAULT",
    "STALE_SPEC",
    "UNREGISTERED",
    "StartFailure",
    "classify_start_failure",
]
