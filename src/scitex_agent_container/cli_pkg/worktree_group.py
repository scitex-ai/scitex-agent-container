"""``sac worktree`` noun group — git-worktree hygiene on this host.

A new top-level noun rather than a fold-in, because worktree sprawl is
REPO-scoped, not agent-scoped. The closest existing homes are all the
wrong shape: ``sac agents`` verbs take an agent name and act on that
agent's runtime; ``sac db clean`` is the state DB; ``sac host`` is peer
identity/routing. A repo's worktrees belong to none of them — they
outlive the agent that made them, which is precisely why they sprawl.

Verbs are attached by their own modules' ``register()`` (see
:mod:`._worktree_gc`), mirroring how ``host_group`` collects ``_host_sync``
and ``_host_crud``.
"""

from __future__ import annotations

import click

from . import _worktree_gc


@click.group(
    "worktree",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def worktree_group() -> None:
    """Git-worktree hygiene: report and reap safe, stale worktrees.

    \b
    Examples:
      $ sac worktree gc --repo ~/proj/scitex-todo    # read-only report
      $ sac worktree gc --apply --all                # act
    """


_worktree_gc.register(worktree_group)

__all__ = ["worktree_group"]
