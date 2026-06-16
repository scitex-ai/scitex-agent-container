"""CI-verdict delivery orchestration (sac #404).

feedback.pdf §3: "a2a-deliver the verdict to the pusher, then up the
recorded lineage pusher → parent → … → lead. Job = delivery." This is
the composition layer that ties the data pieces together:

  dedup-guard → resolve owner → deliver to pusher → climb to lead → record

Only TERMINAL verdicts (``success`` / ``failure``) are delivered; a
``pending`` / ``none`` conclusion is a no-op so the verdict is delivered
exactly once it actually resolves (and the dedup key is recorded only
after a real terminal delivery, so a later red→green flip still fires).

Every collaborator (``post`` transport, owner resolver, lineage walk,
dedup read/write) is a keyword seam defaulting to the production impl —
so the poll loop calls it with no args and tests inject fakes (no
network / gh / state.db). A per-target ``post`` failure is logged and
skipped; the climb continues and the verdict is still recorded (the
agent learns of the verdict on its next heartbeat/poll regardless).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL_CONCLUSIONS = frozenset({"success", "failure"})


def _verdict_text(repo: str, pr: int, head_sha: str, conclusion: str) -> str:
    short = head_sha[:8] if head_sha else "?"
    head = f"CI {conclusion.upper()} — {repo} PR #{pr} ({short})."
    if conclusion == "success":
        tail = " Green: self-merge if you own it. Do NOT poll `gh pr checks`."
    else:
        tail = " Red: fix-and-push — the ring re-fires on your next push."
    return head + tail


def deliver_verdict(
    repo: str,
    pr: int,
    head_sha: str,
    conclusion: str,
    *,
    post: Any = None,
    owner_resolver: Any = None,
    ancestors: Any = None,
    already_delivered: Any = None,
    record: Any = None,
    db_path: Any = None,
    agents_dir: Any = None,
    tasks_path: Any = None,
    pr_body: str | None = None,
) -> dict:
    """Deliver one CI verdict to its owner + up the lineage. Idempotent.

    Returns a summary ``{"delivered": [names], "skipped": bool,
    "reason": str}``. ``reason`` ∈ {``delivered``, ``non-terminal``,
    ``already-delivered``, ``no-owner``}.
    """
    if conclusion not in TERMINAL_CONCLUSIONS:
        return {"delivered": [], "skipped": True, "reason": "non-terminal"}

    # Bind production defaults lazily (None seam → real impl) to keep
    # import cost off the hot path and avoid import cycles.
    if post is None:
        from .._network.peer import post_turn

        post = post_turn
    if owner_resolver is None:
        from ._ci_owner import resolve_owner

        owner_resolver = resolve_owner
    if ancestors is None:
        from .._state._lineage import ancestors_to_root

        ancestors = ancestors_to_root
    if already_delivered is None:
        from .._state.state_db_verdict_dedup import verdict_already_delivered

        already_delivered = verdict_already_delivered
    if record is None:
        from .._state.state_db_verdict_dedup import record_verdict_delivered

        record = record_verdict_delivered

    if already_delivered(
        repo=repo, pr=pr, head_sha=head_sha, conclusion=conclusion, db_path=db_path
    ):
        return {"delivered": [], "skipped": True, "reason": "already-delivered"}

    owner = owner_resolver(
        repo, pr_body=pr_body, agents_dir=agents_dir, tasks_path=tasks_path
    )
    if not owner:
        return {"delivered": [], "skipped": True, "reason": "no-owner"}

    targets = [owner, *ancestors(name=owner, db_path=db_path)]
    text = _verdict_text(repo, pr, head_sha, conclusion)
    delivered: list[str] = []
    for target in targets:
        try:
            post(target, text)
            delivered.append(target)
        except Exception as exc:  # stx-allow: fallback (one unreachable target must not abort the climb)
            logger.warning("deliver_verdict: post to %s failed: %s", target, exc)

    # Record AFTER attempting delivery so a verdict that found an owner is
    # not re-delivered next tick. A total-delivery failure still records —
    # the agent picks the verdict up on its own heartbeat; re-spamming a
    # transiently-unreachable fleet every tick is worse than one miss.
    record(repo=repo, pr=pr, head_sha=head_sha, conclusion=conclusion, db_path=db_path)
    return {"delivered": delivered, "skipped": False, "reason": "delivered"}


__all__ = ["TERMINAL_CONCLUSIONS", "deliver_verdict"]
