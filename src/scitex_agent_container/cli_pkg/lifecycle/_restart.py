#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents restart`` — node-aware stop-then-start of agent(s).

Accepts ONE OR MORE agent names (mirroring ``sac agents start``) plus an
``--all`` flag that restarts every agent ``sac agents list`` shows. Each
name is restarted independently: one agent failing does not abort the
rest, and the command exits non-zero if ANY restart failed.

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


def _should_try_host_bypass(exc: Exception) -> bool:
    """Return True iff a LOCAL restart failure should fall back to the host.

    The fallback fires only when BOTH hold:

      * the failure is the "not found in registry" local-resolution miss
        (an in-SIF agent cannot see a peer's bare-host registry row), and
      * ``SAC_LISTEN_BASE_URL`` is set (we are a container with the host
        listen reachable — the spawn bypass's precondition).

    Any other RuntimeError (a real restart fault on a resolvable agent)
    propagates unchanged so the bare-host operator path is untouched.
    """
    from ..._lifecycle._restart_client import RestartRequestError, _resolve_base_url

    if "not found in registry" not in str(exc):
        return False
    try:
        _resolve_base_url(None)
    except RestartRequestError:
        return False
    return True


def _restart_via_host_bypass(name: str, fresh: bool = False) -> dict:
    """Broker the restart to the HOST listen and return its JSON envelope.

    Mirrors the spawn bypass (``agent_spawn`` → ``request_spawn``): the
    in-SIF client POSTs to ``{SAC_LISTEN_BASE_URL}/agents/<name>/restart``
    and the host runs ``sac agents restart <name> --yes`` (or, when
    ``fresh``, ``sac agents start <name> --force --fresh``) on the bare host
    (manage-gated by ``check_lineage_acl``). A :class:`RestartRequestError`
    (transport / 401 / 403 / 5xx) propagates so the CLI's outer ``except``
    surfaces it fail-loud.
    """
    from ..._lifecycle._restart_client import request_restart

    return request_restart(name, fresh=fresh)


def _bypass_base_url_available() -> bool:
    """True iff a host-listen base URL resolves (we are an in-container agent).

    The fresh-restart path is bypass-only: it has nothing to broker to on a
    bare host, so the CLI fails loud there rather than silently doing a
    resuming restart.
    """
    from ..._lifecycle._restart_client import RestartRequestError, _resolve_base_url

    try:
        _resolve_base_url(None)
    except RestartRequestError:
        return False
    return True


def _enumerate_fleet() -> list[str]:
    """Return every agent name ``sac agents list`` shows (the ``--all`` set).

    Reuses the SAME data function the ``list`` command uses
    (:func:`cli_pkg._helpers.get_agent_list_data`) so ``--all`` is exactly
    "everything ``sac agents list`` shows" — registered/running agents plus
    on-disk-defined ones — with no separate enumeration path to drift.
    Order-preserving de-dup by name.
    """
    from .._helpers import get_agent_list_data
    from ..._state.registry import Registry

    seen: set[str] = set()
    names: list[str] = []
    for row in get_agent_list_data(Registry()):
        name = row.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _restart_one(name: str, *, as_json: bool, fresh: bool) -> tuple[dict, bool]:
    """Restart ONE agent; return ``(json_envelope, ok)``.

    Preserves every single-name path — cross-host ssh dispatch, the
    ``--fresh`` host-bypass, and the local→host-listen fallback. Human
    console output is printed here when ``not as_json`` (byte-for-byte the
    historical single-name output); JSON emission is left to the CALLER so
    it can choose a bare object (single name) vs an array (batch / --all).
    Never raises for an ordinary restart fault and never calls ``sys.exit``
    — the caller aggregates the batch exit code.
    """
    if fresh:
        # Fresh restart is the in-container recovery path: broker
        # ``start --force --fresh`` to the host listen. On a bare host there is
        # no listen to broker to, so fail loud with the direct command rather
        # than silently doing a resuming restart (which would re-wedge).
        if not _bypass_base_url_available():
            msg = (
                f"--fresh restart requires the host bypass (run inside a "
                f"container). On a bare host run: sac agents start {name} "
                f"--force --fresh"
            )
            if not as_json:
                click.echo(msg, err=True)
            return {"name": name, "error": msg, "fresh": True}, False
        envelope = _restart_via_host_bypass(name, fresh=True)
        out = {
            "name": name,
            "restarted": envelope.get("returncode") == 0,
            "dispatched": False,
            "via": "host-listen",
            "fresh": True,
            "host_response": envelope,
        }
        if not as_json:
            console.print(
                f"[green]Agent '{name}' fresh-restarted via host listen[/green]"
            )
        return out, bool(out["restarted"])
    # stx-allow: fallback (reason: config resolution, cross-host ssh dispatch, or
    # agent_restart can raise if the agent is not running or the session cannot be
    # found; an error envelope is cleaner than an unhandled traceback)
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
            out = {
                "name": name,
                "restarted": True,
                "host": envelope_holder.get("_peer"),
                "a2a_port": envelope_holder.get("a2a_port"),
                "dispatched": True,
            }
            if not as_json:
                console.print(
                    f"[green]Agent '{name}' restarted on "
                    f"'{envelope_holder.get('_peer')}'[/green]"
                )
            return out, True

        # Local restart (row on this host, or no row — spec fallback).
        # When that LOCAL resolution fails (no registry row AND no
        # resolvable spec) AND we are inside a container with the host
        # listen reachable (SAC_LISTEN_BASE_URL injected), broker the
        # restart to the HOST listen — exactly like the spawn bypass.
        try:
            agent_restart(name)
        except RuntimeError as exc:
            if not _should_try_host_bypass(exc):
                raise
            envelope = _restart_via_host_bypass(name)
            out = {
                "name": name,
                "restarted": envelope.get("returncode") == 0,
                "dispatched": False,
                "via": "host-listen",
                "host_response": envelope,
            }
            if not as_json:
                console.print(
                    f"[green]Agent '{name}' restarted via host listen[/green]"
                )
                console.print(_json.dumps(envelope))
            return out, bool(out["restarted"])
        out = {"name": name, "restarted": True, "dispatched": False}
        if not as_json:
            console.print(f"[green]Agent '{name}' restarted[/green]")
        return out, True
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not as_json:
            console.print(f"[red]Error: {exc}[/red]")
        return {"name": name, "error": str(exc)}, False


@click.command()
@click.argument(
    "names",
    metavar="NAME...",
    nargs=-1,
    required=False,
    shell_complete=agent_name_complete,
)
@click.option(
    "--all",
    "all_agents",
    is_flag=True,
    default=False,
    help=(
        "Restart EVERY agent 'sac agents list' shows. Mutually exclusive "
        "with explicit NAME arguments. Still requires -y/--yes."
    ),
)
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
@click.option(
    "--fresh",
    "fresh",
    is_flag=True,
    default=False,
    help=(
        "Start a NEW Claude session instead of resuming (brokers "
        "'start --force --fresh' to the host). The deterministic recovery for "
        "an agent wedged on a boot prompt whose queued input keeps returning "
        "on a plain restart. In-container only; on a bare host run "
        "'sac agents start <name> --force --fresh' directly."
    ),
)
def restart(
    names: tuple[str, ...],
    all_agents: bool,
    dry_run: bool,
    yes: bool,
    as_json: bool,
    fresh: bool,
) -> None:
    """Restart one or more agents.

    Pass one or more NAMEs, or ``--all`` to restart every agent
    ``sac agents list`` shows. For each name, the agent's recorded host is
    resolved first: a row on a remote peer is restarted over ssh on that
    peer (node-aware); otherwise the restart runs locally. Agents are
    restarted independently — one failing does not abort the rest, and the
    command exits non-zero if ANY restart failed.

    \b
    Example:
      $ sac agents restart foo -y
      $ sac agents restart foo bar baz -y     # several in one call
      $ sac agents restart --all -y           # every registered agent
      $ sac agents restart foo --dry-run
      $ sac agents restart foo --json
    """
    if all_agents and names:
        raise click.UsageError(
            "--all cannot be combined with explicit agent NAME arguments."
        )

    if all_agents:
        targets = _enumerate_fleet()
        if not targets:
            if as_json:
                click.echo(_json.dumps([]))
            else:
                console.print("[dim]No agents found to restart.[/dim]")
            return
    else:
        targets = list(names)

    if not targets:
        raise click.UsageError(
            "Missing argument 'NAME...'. Pass one or more agent names, or --all."
        )

    if dry_run:
        for name in targets:
            click.echo(f"[dry-run] would restart agent '{name}'")
        return

    if not yes:
        if len(targets) == 1:
            click.echo(
                f"Refusing to restart agent '{targets[0]}' without --yes/-y.",
                err=True,
            )
        else:
            click.echo(
                f"Refusing to restart {len(targets)} agents without --yes/-y.",
                err=True,
            )
        raise SystemExit(2)

    results: list[dict] = []
    any_failed = False
    for name in targets:
        envelope, ok = _restart_one(name, as_json=as_json, fresh=fresh)
        results.append(envelope)
        if not ok:
            any_failed = True

    if as_json:
        # Backward-compat: a SINGLE explicit name emits a bare object;
        # multiple names or --all emit a JSON array of per-agent envelopes.
        if len(results) == 1 and not all_agents:
            click.echo(_json.dumps(results[0]))
        else:
            click.echo(_json.dumps(results))

    if any_failed:
        sys.exit(1)


__all__ = ["restart"]
