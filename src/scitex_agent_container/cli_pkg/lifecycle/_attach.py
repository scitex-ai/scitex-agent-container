"""``sac agents attach <name>`` — attach your terminal to a running agent.

The TUI runtime holds each agent's interactive ``claude`` process in a
DETACHED tmux session named by :func:`session_name_for` (currently
``tui-<name>``). This verb resolves that session and hands your terminal to
``tmux attach`` (via ``execvp``) so you can watch and drive the live agent;
detach again with the usual tmux keys (``Ctrl-b d``).

**Cross-host (control-plane) attach.** The operator drives the whole fleet
from the master, regardless of where an agent actually runs. So when the
agent's ``spec.host`` classifies as a *remote* peer (the same
``classify_dispatch_host`` resolution ``sac agents start`` dispatches
through), attach does not look at the LOCAL tmux — it ``ssh -t``'s to that
peer and runs ``tmux attach`` there. The remote agent's session lives on the
peer's default tmux server and persists across attach/detach, so the
operator gets the live remote agent from their local terminal; ``Ctrl-b d``
detaches and drops back to the peer shell, ``Ctrl-d`` returns to the master.

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


def _classify_agent_host(name: str) -> tuple[str, str | None]:
    """Return ``(kind, peer)`` for the agent's ``spec.host``.

    ``kind`` is ``"local"`` / ``"remote"`` / ``"unknown"`` per
    :func:`._common.classify_dispatch_host` — the SAME resolver
    ``sac agents start`` uses, so attach and start agree on where an agent
    lives. Best-effort: an unresolvable spec (moved / deleted) degrades to
    ``("local", None)`` so attach still tries the local tmux, preserving the
    historic single-host behaviour.
    """
    try:
        from ..._state.host_config import load as _load_host_config
        from ...config import load_config
        from ...config._host import resolve_hostname
        from ...config._resolve import resolve_with_prefix
        from ._common import _local_host_names, classify_dispatch_host

        config = load_config(resolve_with_prefix(name))
        bound = config.hosts_spec.host
        target = bound if isinstance(bound, str) else (bound[0] if bound else None)
        if not target:
            return ("local", None)
        current = resolve_hostname()
        peers = _load_host_config().peers
        return classify_dispatch_host(
            target, current, peers, local_names=_local_host_names(current)
        )
    except Exception:  # stx-allow: fallback (unresolved spec/config → local attach)
        return ("local", None)


def _remote_attach_argv(session: str, peer: str) -> list[str]:
    """Build the ``ssh -t <target> tmux attach -t <session>`` argv for a remote agent.

    The ssh alias comes from ``host_config.peers[peer].ssh`` (the same
    source ``sac agents start`` dispatches through); it falls back to the
    peer name when no explicit ``ssh:`` is set. ``tmux`` is invoked DIRECTLY on
    the peer's non-login PATH: a login shell (``bash -lc``) triggers the
    profile's interactive-tmux and fails with "open terminal failed: not a
    terminal", so attach must not wrap the command. ``-t`` still forces a PTY so
    tmux gets a terminal.
    """
    ssh_target = peer
    try:
        from ..._state.host_config import load as _load_host_config

        spec = _load_host_config().peers.get(peer)
        if spec is not None and getattr(spec, "ssh", None):
            ssh_target = spec.ssh
    except (
        Exception
    ):  # stx-allow: fallback (peer without an ssh alias → use the peer name)
        pass
    return [
        "ssh",
        "-t",
        "-o",
        "StrictHostKeyChecking=accept-new",
        ssh_target,
        "tmux",
        "attach",
        "-t",
        session,
    ]


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
def attach(name: str) -> None:
    """Attach your terminal to a running agent's TUI (tmux) session.

    Works whether the agent runs locally or on a remote peer host — a
    remote agent is attached over ssh, so you drive the whole fleet from
    the master.

    \b
    Example:
      $ sac agents attach neurovista     # local; Ctrl-b d to detach
      $ sac agents attach spartan-dev    # remote (Spartan) over ssh
    """
    agent, session = _session_for(name)
    kind, peer = _classify_agent_host(name)

    if kind == "remote" and peer is not None:
        # Control-plane attach: the session lives on the peer, not here.
        system_msg(
            f"'{agent}' runs on remote host '{peer}'; attaching over ssh. "
            f"Ctrl-b d detaches (back to {peer}'s shell); Ctrl-d returns here.",
            style="cyan",
        )
        # Hand the terminal to ssh (replaces this process). A missing remote
        # session surfaces as tmux's own "can't find session" over the PTY —
        # loud, not silent.
        os.execvp("ssh", _remote_attach_argv(session, peer))
        return  # unreachable (execvp replaced the process)

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
