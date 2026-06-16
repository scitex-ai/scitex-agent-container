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

    ready = ready_check if ready_check is not None else _default_ready_check
    if not ready():
        logger.error(
            "github_ci_poll_loop: `gh` is not installed/authenticated — "
            "CI-verdict delivery DISABLED. Run `gh auth login` on this host. "
            "(fail-loud: refusing to run a poller that can deliver nothing)"
        )
        return

    if repos_source is None:
        from ._ci_owner import tracked_repos as repos_source
    if list_prs is None:
        from ._github_ci import list_open_prs as list_prs
    if conclusion_for is None:
        from ._github_ci import pr_ci_conclusion as conclusion_for
    if deliver is None:
        from ._ci_deliver import deliver_verdict as deliver

    logger.info("github_ci_poll_loop: starting (poll_interval_s=%.1f)", poll_interval_s)
    try:
        while True:
            try:
                for repo in list(repos_source()):
                    for pr in list_prs(repo):
                        deliver(
                            repo,
                            pr["number"],
                            pr.get("head_sha", ""),
                            conclusion_for(repo, pr["number"]),
                            pr_body=pr.get("body", ""),
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


__all__ = ["DEFAULT_CI_POLL_INTERVAL_S", "github_ci_poll_loop"]
