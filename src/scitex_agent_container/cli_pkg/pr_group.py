"""``sac pr`` noun group — pull-request lifecycle as a mechanism.

A new top-level noun rather than a fold-in: a PR is REPO-scoped and outlives
whichever agent opened it, so it belongs to none of the existing nouns
(``sac agents`` verbs take an agent name, ``sac db`` is the state DB,
``sac worktree`` is local git hygiene).

ONE verb today, federated as a timer in :mod:`.._jobs_plugin` so it runs
without anyone remembering it:

* ``sync-cards`` — one board card per open PR (facts only; scitex-todo owns
  the nudging)

There is deliberately NO ``close-expired`` verb. The 3-day shelf life is a
FLEET-WIDE rule owned by scitex-dev as a shared primitive, not a sac-local
policy — see :mod:`.._prlifecycle._expiry_seam`.

Exits 0 clean / 1 action needed / 2 COULD-NOT-DETERMINE. The 2 is the point:
see :mod:`.._prlifecycle._gh`.
"""

from __future__ import annotations

import click

from . import _pr_sync_cards


@click.group("pr", context_settings={"help_option_names": ["-h", "--help"]})
def pr_group() -> None:
    """Pull-request lifecycle: make every open PR trackable on the board.

    \b
    Examples:
      $ sac pr sync-cards --check            # read-only: what would be carded
      $ sac pr sync-cards --apply            # upsert one card per open PR

    \b
    Exit codes (all verbs):
      0  clean — and we PROVED we could read the PR list
      1  action needed
      2  COULD NOT DETERMINE (gh unauthenticated / offline / rate-limited).
         Never confused with 0: an unreadable backlog is not an empty one.
    """


_pr_sync_cards.register(pr_group)

__all__ = ["pr_group"]
