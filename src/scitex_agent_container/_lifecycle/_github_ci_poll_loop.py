"""GitHub-CI poll loop — the listen-side glue for the CI ring (sac #404).

feedback.pdf §3: ``sac listen`` polls GitHub CI on its OWN schedule and
a2a-delivers each verdict to the pusher, then up the lineage to lead —
STANDALONE (todo down → sac still delivers; no relay, no SPOF).

This is the long-running asyncio task the listen lifespan launches at
boot (sibling of :func:`_periodic_drive_loop.periodic_drive_loop`).
Each tick: for every tracked repo, list its open PRs and hand each
``(repo, pr, head_sha, conclusion)`` to :func:`_ci_deliver.deliver_verdict`
(which dedups, resolves the owner, delivers, climbs the lineage, records).

Fail-loud preflight (operator: fail-loud, fail-fast, no silent fallbacks):
a missing / unauthenticated ``gh`` is a DEPLOY error, not a transient
blip — the loop logs an ERROR and DISABLES itself rather than emitting a
silent stream of ``none`` verdicts forever.

Loop resilience: a per-tick exception is logged + retried next tick (the
poller must survive a transient GitHub/network error), and cancellation
is honoured at the sleep boundary so a ``sac listen`` SIGTERM doesn't
leak the loop — same contract as the periodic-drive lane.

Every collaborator is an injection seam so tests drive the full loop
deterministically without gh / network / state.db.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Default poll cadence. The verdict already arrives "fast" because dev's
# Spartan CI is ~3 min + auto-merge; a 5-min poll floor keeps GitHub API
# load trivial while staying within the operator's "instant-enough"
# scale. Override via ``SAC_GITHUB_CI_POLL_INTERVAL_S`` at the wiring site.
DEFAULT_CI_POLL_INTERVAL_S = 300.0

#: Per-target budget for delivering ONE verdict.
#:
#: ``post_turn`` defaults to ``timeout_s=600.0``, which is longer than the
#: tick bound below (``max(poll_interval_s, 30.0)``, i.e. 300s by default) —
#: so a single unresponsive peer could outlive the tick that started it. And
#: ``run_blocking_or`` ABANDONS a timed-out call rather than cancelling it,
#: so the abandoned post keeps running while the loop sleeps and starts the
#: next tick: two ticks alive at once, both able to post.
#:
#: Never observed in production, and it structurally cannot be: the
#: delivered-set is keyed on ``(repo, pr, head_sha, conclusion)``, so a
#: duplicate delivery from an overlapping tick is deduped away and leaves no
#: row to find. An absent symptom here is not evidence of absence.
#:
#: A verdict notification is a short POST to a peer on the same fleet. It
#: does not need a ten-minute budget, and bounding it well under the tick
#: keeps the inversion impossible rather than merely unobserved.
VERDICT_POST_TIMEOUT_S = 30.0


def _default_ready_check() -> bool:
    from ._github_ci import gh_ready

    return gh_ready()


async def github_ci_poll_loop(
    *,
    poll_interval_s: float = DEFAULT_CI_POLL_INTERVAL_S,
    repos_source: Any = None,
    list_prs: Any = None,
    conclusion_for: Any = None,
    deliver: Any = None,
    ready_check: Any = None,
) -> None:
    """Long-running CI-verdict poll+deliver task for the listen lifespan.

    Seams (production defaults bound when ``None``): ``repos_source`` →
    :func:`_ci_owner.tracked_repos`, ``list_prs`` →
    :func:`_github_ci.list_open_prs`, ``conclusion_for`` →
    :func:`_github_ci.pr_ci_conclusion`, ``deliver`` →
    :func:`_ci_deliver.deliver_verdict`, ``ready_check`` →
    :func:`_github_ci.gh_ready`.
    """
    if os.environ.get("SAC_GITHUB_CI_POLLER_DISABLED", "") == "1":
        logger.info("github_ci_poll_loop: disabled via SAC_GITHUB_CI_POLLER_DISABLED")
        return

    # ROOT-CAUSE GUARD (cards sac-listen-self-peer-persist-blocks-bind /
    # sac-listen-watchdog-autorestart-alarm): this preflight is the FIRST
    # thing the loop does, BEFORE its first ``await asyncio.sleep`` — and
    # the production ``ready`` is ``gh_ready()`` → ``subprocess.run(['gh',
    # 'auth', 'status'])``, which makes a NETWORK call to GitHub. Run
    # synchronously on the event loop, a hung ``gh``/network here starves
    # uvicorn's bind and silently takes the whole fleet's comms down. So
    # dispatch it off the loop with a hard timeout; a wedged probe degrades
    # to "not ready" (fail-loud) instead of hanging the listen daemon.
    from ._off_loop import run_blocking_or

    ready = ready_check if ready_check is not None else _default_ready_check
    if not await run_blocking_or(ready, default=False, op="gh auth status (gh_ready)"):
        logger.error(
            "github_ci_poll_loop: `gh` is not installed/authenticated (or its "
            "auth probe timed out) — CI-verdict delivery DISABLED. Run "
            "`gh auth login` on this host. (fail-loud: refusing to run a "
            "poller that can deliver nothing)"
        )
        return

    if repos_source is None:
        from ._ci_owner import tracked_repos as repos_source
    if list_prs is None:
        from ._github_ci import list_open_prs as list_prs
    if conclusion_for is None:
        from ._github_ci import pr_ci_conclusion as conclusion_for
    if deliver is None:
        from functools import partial

        from .._network.peer import post_turn
        from ._ci_deliver import deliver_verdict as _deliver_verdict

        _bounded_post = partial(post_turn, timeout_s=VERDICT_POST_TIMEOUT_S)

        def deliver(*args, **kwargs):
            # Bind the bounded transport unless a caller supplied its own.
            kwargs.setdefault("post", _bounded_post)
            return _deliver_verdict(*args, **kwargs)

    def _tick_body() -> None:
        # Each of repos_source / list_prs / conclusion_for / deliver may
        # shell out to ``gh`` (blocking subprocess.run); run the whole
        # tick body off the event loop so a slow GitHub read never starves
        # the listen server even after bind.
        for repo in list(repos_source()):
            for pr in list_prs(repo):
                deliver(
                    repo,
                    pr["number"],
                    pr.get("head_sha", ""),
                    conclusion_for(repo, pr["number"]),
                    pr_body=pr.get("body", ""),
                )

    logger.info("github_ci_poll_loop: starting (poll_interval_s=%.1f)", poll_interval_s)
    try:
        while True:
            try:
                # Bound the tick generously: a full poll across repos can
                # legitimately take a while, but must never run unbounded
                # on (or off) the loop. Timeout → logged + retried.
                await run_blocking_or(
                    _tick_body,
                    default=None,
                    op="github_ci_poll_loop tick (gh reads)",
                    timeout_s=max(poll_interval_s, 30.0),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must survive a transient GitHub/registry error; logged, retried next tick)
                logger.warning(
                    "github_ci_poll_loop: tick failed (%s); retry next tick", exc
                )
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        logger.info("github_ci_poll_loop: cancelled cleanly")
        raise


__all__ = [
    "DEFAULT_CI_POLL_INTERVAL_S",
    "VERDICT_POST_TIMEOUT_S",
    "github_ci_poll_loop",
]
