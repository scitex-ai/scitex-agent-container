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

#: Consecutive reds (no green in between) delivered normally before the ring
#: escalates once and then goes quiet for that PR. Measured 2026-08-16: a
#: standing ``release: sync main with develop`` PR whose head ref IS the source
#: branch collected FOURTEEN "Red: fix-and-push" deliveries in a day. Nobody had
#: pushed at it — each unrelated feature merge moved its head, minting a fresh
#: dedup key. The instruction was unfollowable and the volume taught its
#: recipients to skim.
CONSECUTIVE_FAILURE_CAP = 3


def _verdict_text(repo: str, pr: int, head_sha: str, conclusion: str) -> str:
    short = head_sha[:8] if head_sha else "?"
    head = f"CI {conclusion.upper()} — {repo} PR #{pr} ({short})."
    if conclusion == "success":
        tail = " Green: self-merge if you own it. Do NOT poll `gh pr checks`."
    else:
        tail = " Red: fix-and-push — the ring re-fires on your next push."
    return head + tail


def _escalation_text(
    repo: str,
    pr: int,
    head_sha: str,
    streak: int,
    checks: list | None = None,
) -> str:
    """The one message sent when a PR trips :data:`CONSECUTIVE_FAILURE_CAP`.

    Deliberately does NOT say "fix-and-push". That instruction is what kept
    the measured loop alive, and by this point it is the one thing already
    known not to work.

    ``checks`` names the currently-failing checks when they could be
    resolved. Naming them is the difference between advice the reader can
    act on and advice they cannot: "check whether the failing check is
    required" is unanswerable without knowing which check it is.
    """
    short = head_sha[:8] if head_sha else "?"
    named = f" Failing now: {', '.join(checks)}." if checks else ""
    return (
        f"CI STUCK — {repo} PR #{pr} ({short}). This is red #{streak + 1} "
        f"with no green in between, so the ring is going SILENT for this PR "
        f"until a green verdict lands.{named} Pushing has not cleared it; "
        "check whether that check is a REQUIRED context (a non-required "
        "check cannot block a merge), and whether this PR's head is moving "
        "for reasons unrelated to your changes — a standing sync PR's head "
        "tracks its source branch, so every merge there re-fires CI."
    )


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
    failure_streak: Any = None,
    failing_checks: Any = None,
    db_path: Any = None,
    agents_dir: Any = None,
    pr_body: str | None = None,
) -> dict:
    """Deliver one CI verdict to its owner + up the lineage. Idempotent.

    Returns a summary ``{"delivered": [names], "skipped": bool,
    "reason": str}``. ``reason`` ∈ {``delivered``, ``non-terminal``,
    ``already-delivered``, ``no-owner``, ``escalated``, ``streak-capped``}.

    After :data:`CONSECUTIVE_FAILURE_CAP` reds with no green in between,
    one ``escalated`` message goes out and every further red for that PR
    is ``streak-capped`` — recorded but not delivered — until a green
    resets the streak. The recorded count IS the state, so no extra
    column is needed and a restart cannot lose the position.
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
    if failure_streak is None:
        from .._state.state_db_verdict_dedup import failures_since_last_success

        failure_streak = failures_since_last_success

    if already_delivered(
        repo=repo, pr=pr, head_sha=head_sha, conclusion=conclusion, db_path=db_path
    ):
        return {"delivered": [], "skipped": True, "reason": "already-delivered"}

    # Streak gate — BEFORE resolving an owner, so a capped PR costs no gh call.
    # `streak` counts reds already delivered since the last green, so the first
    # red sees 0. Record even when silent: the count is the state, and letting
    # it stall would un-cap the PR on the next tick.
    escalating = False
    if conclusion == "failure":
        streak = failure_streak(repo=repo, pr=pr, db_path=db_path)
        if streak > CONSECUTIVE_FAILURE_CAP:
            record(
                repo=repo,
                pr=pr,
                head_sha=head_sha,
                conclusion=conclusion,
                db_path=db_path,
            )
            return {"delivered": [], "skipped": True, "reason": "streak-capped"}
        escalating = streak == CONSECUTIVE_FAILURE_CAP

    owner = owner_resolver(repo, pr_body=pr_body, agents_dir=agents_dir)
    if not owner:
        return {"delivered": [], "skipped": True, "reason": "no-owner"}

    targets = [owner, *ancestors(name=owner, db_path=db_path)]
    if escalating:
        # One extra gh call, on the rare escalation tick only — never on the
        # hot path. Fail-soft: an unnamed escalation still beats no escalation.
        if failing_checks is None:
            from ._github_ci import failing_check_names

            failing_checks = failing_check_names
        try:
            checks = failing_checks(repo, pr)
        except Exception as exc:  # stx-allow: fallback (naming is a nicety, not the message)
            logger.warning("deliver_verdict: failing_check_names failed: %s", exc)
            checks = []
        text = _escalation_text(repo, pr, head_sha, streak, checks)
    else:
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
    return {
        "delivered": delivered,
        "skipped": False,
        "reason": "escalated" if escalating else "delivered",
    }


__all__ = [
    "CONSECUTIVE_FAILURE_CAP",
    "TERMINAL_CONCLUSIONS",
    "deliver_verdict",
]
