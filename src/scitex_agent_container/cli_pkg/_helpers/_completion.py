"""Click shell-completion callbacks."""

from __future__ import annotations

import click


def agent_name_complete(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[str]:
    """Click ``shell_complete`` callback that returns matching agent names.

    Resolves names via :func:`scitex_agent_container.config.enumerate_agent_names`
    so completion respects the same search chain as ``sac agent start <name>``:
    project-local → user-wide → ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` →
    fleet directories. Filtered to those whose name starts with the
    operator's partial input.

    Used as ``@click.argument("name", shell_complete=agent_name_complete)``
    on every command that accepts an agent name. Failures (no agents yet,
    file system errors, etc.) silently return an empty list — completion
    must never block typing.
    """
    del ctx, param  # unused: completion is global, not per-command
    try:
        from ...config._resolve import enumerate_agent_names

        names = enumerate_agent_names()
    except Exception:  # stx-allow: fallback (reason: completion must never raise; empty list is the safe fall-through)
        return []
    return [n for n in names if n.startswith(incomplete)]
