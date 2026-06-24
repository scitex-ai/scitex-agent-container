"""``sac agents attach <name>`` — attach your terminal to a running agent.

The TUI runtime holds each agent's interactive ``claude`` process in a
DETACHED tmux session named by :func:`session_name_for` (currently
``tui-<name>``). This verb resolves that session and hands your terminal to
``tmux attach`` (via ``execvp``) so you can watch and drive the live agent;
detach again with the usual tmux keys (``Ctrl-b d``).

Fail-loud: if the agent has no running session, print a red notice with the
next step (``sac agents start <name>``) and exit non-zero — never silently
drop into an empty tmux.
"""

from __future__ import annotations

import os
import subprocess

import click

from .._helpers._completion import agent_name_complete
from .._helpers._console import system_msg


def _session_for(name: str) -> tuple[str, str]:
    """Resolve ``(agent_name, tmux_session)`` for ``name``.

    Prefers the canonical name + session from the loaded spec; falls back to
    the raw name (``tui-<name>``) when the spec can't be resolved, so attach
    still works for an agent whose spec moved.
    """
    try:
        from ...config import load_config
        from ...config._resolve import resolve_with_prefix
        from ...runtimes.tui_session import session_name_for

        config = load_config(resolve_with_prefix(name))
        return config.name, session_name_for(config)
    except Exception:  # stx-allow: fallback (best-effort spec resolution)
        return name, f"tui-{name}"


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
def attach(name: str) -> None:
    """Attach your terminal to a running agent's TUI (tmux) session.

    \b
    Example:
      $ sac agents attach neurovista     # Ctrl-b d to detach
    """
    agent, session = _session_for(name)

    try:
        exists = (
            subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )
    except FileNotFoundError:  # stx-allow: fallback (tmux absent → no session)
        exists = False
    if not exists:
        system_msg(
            f"no running session '{session}' for agent '{agent}'. "
            f"Start it first: `sac agents start {agent}`.",
            style="red",
        )
        raise SystemExit(1)

    # Hand the terminal to tmux (replaces this process; detach with Ctrl-b d).
    os.execvp("tmux", ["tmux", "attach", "-t", session])
