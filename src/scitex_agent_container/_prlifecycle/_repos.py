"""Which repos the two PR jobs sweep.

Precedence, highest first:

1. explicit ``--repo`` options on the command line,
2. ``$SAC_PR_REPOS`` (comma/whitespace separated ``owner/name`` slugs),
3. the built-in default below.

Resolved PER CALL, never cached at import — a module-level constant would bake
the env var before a test could set it (:mod:`.._state.state_paths` documents
the fixture that set ``$HOME`` and silently did nothing).

An EMPTY resolution is propagated as an empty tuple, and both passes treat
"swept zero repos" as :data:`.._sweep.EXIT_UNKNOWN` rather than as clean — "I
examined nothing" is not "nothing is wrong".
"""

from __future__ import annotations

import os

__all__ = ["DEFAULT_REPOS", "REPOS_ENV", "resolve_repos"]

#: Read per call by :func:`resolve_repos`.
REPOS_ENV = "SAC_PR_REPOS"

#: sac's own repo — the one the 2026-07-18 force-close pass was applied to.
DEFAULT_REPOS = ("scitex-ai/scitex-agent-container",)


def _valid(slug: str) -> bool:
    """A GitHub ``owner/name`` slug, minimally shaped.

    Rejecting a malformed slug here is cheap; passing one to ``gh`` produces a
    non-zero exit that the fetch correctly reports as UNREACHABLE — honest, but
    noisier than just not asking.
    """
    parts = slug.split("/")
    return len(parts) == 2 and all(p.strip() for p in parts)


def resolve_repos(explicit=()) -> tuple:
    """Return the repo slugs to sweep, in precedence order."""
    if explicit:
        return tuple(s.strip() for s in explicit if _valid(s.strip()))
    raw = os.environ.get(REPOS_ENV, "")
    if raw.strip():
        candidates = [s for s in raw.replace(",", " ").split() if s.strip()]
        return tuple(s for s in candidates if _valid(s))
    return DEFAULT_REPOS
