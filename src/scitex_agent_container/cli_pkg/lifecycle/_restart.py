#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents restart`` — node-aware stop-then-start of a single agent.

Cross-host dispatch: when an agent's active ``state.db.instances`` row
records ``host != current_host``, ``restart`` ssh's into that peer and
runs ``sac agents restart <name> --yes --json`` there — on the node
where the agent actually runs and where that node's ``sac listen`` bus
token lives. This is the node-aware automation of the working manual
recipe (``stop --yes`` then ``start --yes``, run on the agent's node).
See ``_dispatch.try_dispatch_remote``.

Locally (or when the row lives on the current host), it delegates to
:func:`._lifecycle.lifecycle.agent_restart`, which resolves the spec
from the registry row OR — for ad-hoc-launched agents with no row —
from the standard discovery chain, so a pre-autorecord agent restarts
instead of hard-failing with "not found in registry".
"""

from __future__ import annotations

import json as _json
import shlex
import subprocess
import sys

import click

from ..._lifecycle.lifecycle import agent_restart
from ..._state.host_config import build_ssh_argv
from ..._state.host_config import load as _load_host_config
from ..._state.state_db import record_instance_start, record_instance_stop
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._dispatch import try_dispatch_remote


def _dispatch_remote_restart(peer: str, row: dict, peers: dict, name: str) -> dict:
    """SSH into ``peer`` and run ``sac agents restart <name> --yes --json``.

    The remote restart closes the agent's old instance row and opens a
    fresh one on the peer. Mirror that on the lead side: close the stale
    lead-side row (``record_instance_stop``) and open a new ``remote``
    row carrying the peer-reported bound port so cross-host listings and
    ``resolve_peer_url`` keep pointing at the right node + port.

    Raises ``RuntimeError`` with the full ssh argv + stderr on failure
    (no-silent-fallback rule). Returns the parsed JSON envelope from the
    peer's stdout.
    """
    ssh_argv = build_ssh_argv(
        peer,
        ["sac", "agents", "restart", name, "--yes", "--json"],
        peers,
    )
    result = subprocess.run(
        ssh_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Remote `sac agents restart {name}` failed on {peer!r} "
            f"(rc={result.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        envelope = _json.loads(result.stdout)
    except _json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Remote `sac agents restart {name}` on {peer!r} returned "
            f"non-JSON stdout (peer sac may be too old to support "
            f"--json; pull latest on the peer):\n"
            f"stdout (first 500 chars):\n{result.stdout[:500]}\n"
            f"json error: {exc}"
        ) from exc

    # Close the stale lead-side row, then open a fresh remote row so the
    # restarted agent stays addressable cross-host.
    instance_id = row.get("id")
    if instance_id:
        record_instance_stop(str(instance_id), exit_reason="restarted")
    bound = envelope.get("a2a_port") if isinstance(envelope, dict) else None
    record_instance_start(
        name=name,
        host=peer,
        a2a_port=bound,
        bound_port=bound,
        remote=True,
    )
    return envelope if isinstance(envelope, dict) else {}


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be restarted without making changes.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit a structured JSON envelope on stdout. "
        "Required for cross-host dispatch — the lead parses peer stdout."
    ),
)
def restart(name: str, dry_run: bool, yes: bool, as_json: bool) -> None:
    """Restart an agent.

    Resolves the agent's recorded host first: a row on a remote peer is
    restarted over ssh on that peer (node-aware); otherwise the restart
    runs locally.

    \b
    Example:
      $ sac agent restart foo
      $ sac agent restart foo --dry-run
      $ sac agent restart foo --json
    """
    if dry_run:
        click.echo(f"[dry-run] would restart agent '{name}'")
        return
    if not yes:
        click.echo(f"Refusing to restart agent '{name}' without --yes/-y.", err=True)
        raise SystemExit(2)
    # stx-allow: fallback (reason: config resolution, cross-host ssh dispatch, or
    # agent_restart can raise if the agent is not running or the session cannot be
    # found; an error message + sys.exit(1) is cleaner than an unhandled traceback)
    try:
        if "/" in name or name.endswith((".yaml", ".yml")):
            config_path = resolve_with_prefix(name)
            config = load_config(config_path)
            name = config.name

        # Cross-host: dispatch to the peer holding the agent's active row.
        peers = _load_host_config().peers
        envelope_holder: dict = {}

        def _handler(peer, row, ps, _name=name, _holder=envelope_holder):
            _holder.update(_dispatch_remote_restart(peer, row, ps, _name))
            _holder["_peer"] = peer

        dispatched = try_dispatch_remote(name, "restart", peers, handler=_handler)
        if dispatched:
            if as_json:
                click.echo(
                    _json.dumps(
                        {
                            "name": name,
                            "restarted": True,
                            "host": envelope_holder.get("_peer"),
                            "a2a_port": envelope_holder.get("a2a_port"),
                            "dispatched": True,
                        }
                    )
                )
            else:
                console.print(
                    f"[green]Agent '{name}' restarted on "
                    f"'{envelope_holder.get('_peer')}'[/green]"
                )
            return

        # Local restart (row on this host, or no row — spec fallback).
        agent_restart(name)
        if as_json:
            click.echo(
                _json.dumps({"name": name, "restarted": True, "dispatched": False})
            )
        else:
            console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if as_json:
            click.echo(_json.dumps({"name": name, "error": str(exc)}))
        else:
            console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


__all__ = ["restart"]
