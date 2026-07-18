"""PR lifecycle as a MECHANISM, not a rule someone has to remember.

ONE job ships here, federated as a systemd timer in :mod:`.._jobs_plugin`:

* ``sac.sync-pr-cards`` (:mod:`._sweep`) — one board card per open PR, so the
  backlog is TRACKABLE. Completes the card when the PR merges or closes. It
  contains NO nudge logic: scitex-todo's stale-active sweep already owns
  nudging, and sac owns the PR facts it nudges about. That split is the point —
  two nudgers would race; a nudger with nothing to nudge about is the gap that
  let 35 PRs pile up.

The 3-day EXPIRY is deliberately NOT here. It is a FLEET-WIDE rule that
scitex-dev owns as a shared primitive (operator, 2026-07-18: 「3日ルールは全て
のレポジトリで共通です」), and sac implementing its own would fork it. See
:mod:`._expiry_seam` for the seam, the consumption contract sac will hold that
primitive to, and why nothing was left behind as a placeholder.

Everything that DOES ship is TRI-STATE (0 clean / 1 action needed / 2
could-not-determine) and the 2 is the reason the package is shaped the way it
is. See :mod:`._gh` for the one rule everything else follows: an EMPTY open-PR
list and an UNREADABLE one are different facts, and rendering the second as the
first would show a 35-PR backlog as a clean board.
"""

from __future__ import annotations

from ._cards import CARD_ID_PREFIX, HEARTBEAT_CARD_ID, MANIFEST_CARD_ID, card_id_for
from ._expiry_seam import HANDOFF_CARD_ID, PRIMITIVE_OWNER
from ._gh import (
    FetchState,
    GhInvocation,
    PRFetch,
    PullRequest,
    fetch_open_prs,
    parse_pr_rows,
)
from ._repos import DEFAULT_REPOS, resolve_repos
from ._sweep import EXIT_ACTION, EXIT_CLEAN, EXIT_UNKNOWN, SweepOutcome, sync_cards

__all__ = [
    "CARD_ID_PREFIX",
    "DEFAULT_REPOS",
    "EXIT_ACTION",
    "EXIT_CLEAN",
    "EXIT_UNKNOWN",
    "FetchState",
    "GhInvocation",
    "HANDOFF_CARD_ID",
    "HEARTBEAT_CARD_ID",
    "MANIFEST_CARD_ID",
    "PRIMITIVE_OWNER",
    "PRFetch",
    "PullRequest",
    "SweepOutcome",
    "card_id_for",
    "fetch_open_prs",
    "parse_pr_rows",
    "resolve_repos",
    "sync_cards",
]
