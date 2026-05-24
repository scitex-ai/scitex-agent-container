"""``sac peer`` noun-group — outbound A2A calls into other agents' /v1/turn.

Mirrors the Python surface in :mod:`scitex_agent_container._network.peer`. Two
verbs today:

* ``sac peer post-turn <agent> "<text>"`` — send one user turn to a
  peer agent (local or remote via ssh-as-transport), print the reply.
* ``sac peer resolve-url <agent>`` — print the URL ``post-turn`` would
  POST to, without sending. Useful for ops debugging.

Noun-verb pattern per ``general/03_interface_02_cli/02_subcommand-structure-noun-verb.md``:
``peer`` is the noun group, ``post-turn`` and ``resolve-url`` are the
verbs (compound with hyphen). Tree form chosen because the noun has 2+
sibling verbs (with room to grow — list / health-check / etc.).
"""

from __future__ import annotations

import json
import sys

import click

from ._helpers._completion import agent_name_complete


@click.group(name="peer")
def peer_group() -> None:
    """Outbound A2A calls into other agents' POST /v1/turn endpoint."""


@peer_group.command(name="post-turn")
@click.argument("agent_name", metavar="AGENT", shell_complete=agent_name_complete)
@click.argument("text")
@click.option(
    "--exit-after",
    is_flag=True,
    default=False,
    help="Tell the peer runner to shut down after this turn (CI smokes use this).",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=600.0,
    show_default=True,
    help="Per-turn timeout in seconds.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full JSON envelope instead of just the reply text.",
)
def peer_post_turn(
    agent_name: str, text: str, exit_after: bool, timeout_s: float, as_json: bool
) -> None:
    """Send one user turn to AGENT's /v1/turn; print the reply.

    Resolves AGENT's URL via the standard discovery chain (project-local
    → ~/.scitex/agent-container/agents/ → env → fleet dirs). Remote
    agents (``spec.host`` pinned to a different host) are reached via
    that host's reachable address; local agents go straight to
    127.0.0.1:<port>.

    Examples:
        sac peer post-turn worker "summarize today's commits"
        sac peer post-turn head-mba "..." --exit-after
        sac peer post-turn worker "ping" --json | jq -r .text
    """
    from scitex_agent_container._network.peer import (
        PeerError,
        PeerTimeoutPending,
        post_turn,
    )

    try:
        text_out = post_turn(
            agent_name, text, exit_after=exit_after, timeout_s=timeout_s
        )
    except PeerTimeoutPending as pending:
        # A 504-wait-elapsed is NOT a failure — the turn is likely still
        # running on the peer. Surface the neutral interpretation and
        # exit 0 so callers don't treat in-progress as an error.
        if as_json:
            click.echo(json.dumps(pending.raw_body or {"status": pending.status}))
        else:
            click.echo(pending.interpretation)
        sys.exit(0)
    except PeerError as exc:
        click.echo(f"peer error: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps({"text": text_out, "exit_after": exit_after}))
    else:
        click.echo(text_out)


@peer_group.command(name="resolve-url")
@click.argument("agent_name", metavar="AGENT", shell_complete=agent_name_complete)
def peer_resolve_url(agent_name: str) -> None:
    """Print the /v1/turn URL ``peer post-turn`` would POST to for AGENT.

    Doesn't send anything — useful for ops debugging, ssh-tunnel setup,
    or dispatching the call from a different language than Python.

    Example:
        $ sac peer resolve-url head-mba
        ssh://mba:18890/v1/turn
    """
    from scitex_agent_container._network.peer import PeerError, resolve_peer_url

    try:
        url = resolve_peer_url(agent_name)
    except PeerError as exc:
        click.echo(f"peer error: {exc}", err=True)
        sys.exit(2)
    click.echo(url)


__all__ = ["peer_group"]
