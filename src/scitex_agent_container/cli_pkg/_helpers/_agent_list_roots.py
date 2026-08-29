"""User-scope ``agents/`` roots for the ``sac agents list`` disk walk.

One responsibility: answer "which directories hold agent specs for this
process?" — and answer it through the SAME resolver the rest of sac uses,
so ``sac agents list`` cannot disagree with ``sac agents find`` about which
agents exist.

WHY THIS MODULE EXISTS (measured 2026-08-23, scitex-compute-04)

``_agent_list_discover`` hardcoded ``Path.home() / ".scitex" /
"agent-container" / "agents"``. That is correct on a bare host and WRONG
inside a container: there ``Path.home()`` is the agent's own home
(``/home/agent``), while the operator's specs live in a bind-mounted
``/home/ywatanabe``. The container home has no ``agents/`` tree at all, so
the walk skipped its only real root and produced ZERO defined rows.

The runtime already exports ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` pointing
at the operator's tree for exactly this reason, and
:func:`scitex_agent_container.config._resolve._search_dirs` already honours
it — which is why ``sac agents find`` and ``sac agents start`` see those
agents. Only this walk did not, so two commands in one CLI disagreed about
what exists.

The visible symptom: from inside a container the fleet listing reported ONE
local row (a project-local test fixture, ``sdk-test``) while
``/home/ywatanabe/.scitex/agent-container/agents`` held 121 ``spec.yaml``
and the peer legs — which reach other hosts over ssh, landing as the
operator with the right ``$HOME`` — reported 121 and 131. Every locally
running agent therefore read as merely "defined", INCLUDING THE AGENT
MAKING THE QUERY, which is the property that makes the listing unsafe to
reason about: an instrument that cannot see the hand holding it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

__all__ = ["user_scope_roots"]


def _legacy_home_root() -> Path:
    """The pre-2026-08-23 hardcoded root.

    Kept as the fallback so that a resolver failure degrades to the OLD
    behaviour (find the host's agents when running on a bare host) rather
    than to NO behaviour. Discovering the historical root beats discovering
    nothing.
    """
    return Path.home() / ".scitex" / "agent-container" / "agents"


def user_scope_roots(
    search_dirs: Callable[[], tuple[Path, list[Path], list[Path]]] | None = None,
) -> list[Path]:
    """Return the user-scope ``agents/`` roots, highest priority first.

    Delegates to :func:`config._resolve._search_dirs`, which returns
    ``(primary, env_dirs, fleet_dirs)`` and folds in
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``. Order is preserved from the
    resolver; callers de-duplicate by agent name, so a root appearing twice
    is harmless.

    Non-existent roots are NOT filtered here — the caller already skips a
    root that is not a directory, and dropping them silently would hide the
    very condition this module was written to expose.

    ``search_dirs`` is the injection seam for the resolver. Production passes
    nothing and gets the real one; a caller that needs to exercise the
    fallback branch supplies its own. It is a PARAMETER rather than something
    a test rewrites at runtime, so the failure path is reached the same way
    in a test as it would be in production.
    """
    # stx-allow: fallback (reason: see _legacy_home_root — a resolver
    # import/resolution failure must degrade to the historical root, not to
    # an empty search that would silently report zero agents.)
    try:
        if search_dirs is None:
            from ...config._resolve import _search_dirs as search_dirs
        primary, env_dirs, fleet_dirs = search_dirs()
        return [primary, *env_dirs, *fleet_dirs]
    except Exception:  # stx-allow: fallback (reason: see comment above)
        return [_legacy_home_root()]
