#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents restart`` — node-aware stop-then-start of agent(s).

Accepts ONE OR MORE agent names plus a selection flag: ``--all-running``
(only the live fleet), ``--all-registry`` (every registered agent), and
``--all`` (back-compat alias for ``--all-registry``). Each name restarts
independently; the command exits non-zero if ANY restart failed.

Cross-host dispatch: an active ``state.db.instances`` row with ``host !=
current_host`` routes the restart over ssh to that peer (``sac agents
restart <name> --yes --json`` on the node that runs the agent — see
``_dispatch.try_dispatch_remote``). When NO row exists at all, the SPEC's
``host:`` pin routes instead (``_host_routing.spec_host_fallback_peer`` —
transparent remote routing, operator directive 2026-07-10); a pin naming
an UNREGISTERED host fails loud with the registered-peer list.

Locally (row on this host, or an unpinned spec), it delegates to
:func:`._lifecycle.lifecycle.agent_restart`, which resolves the spec from
the registry row OR the standard discovery chain, so a pre-autorecord
agent restarts instead of hard-failing with "not found in registry".
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
from ._host_routing import spec_host_fallback_peer


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
    """Return every agent name ``sac agents list`` shows (the ``--all-registry`` set).

    Reuses the SAME data function the ``list`` command uses
    (:func:`cli_pkg._helpers.get_agent_list_data`) so ``--all-registry`` is
    exactly "everything ``sac agents list`` shows" — registered/running
    agents plus on-disk-defined ones — with no separate enumeration path to
    drift. Order-preserving de-dup by name.
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


def _enumerate_running() -> list[str]:
    """Return only the agents that are currently RUNNING (the ``--all-running`` set).

    Reuses the SAME data function — and therefore the SAME liveness — the
    ``list`` / ``status`` commands use
    (:func:`cli_pkg._helpers.get_agent_list_data`), keeping only rows whose
    ``status`` probe read ``"running"`` (identity-based liveness: the
    session exists AND its pane process is alive — see
    ``_agent_list._probe_local``). Rows that are ``stopped`` / ``unknown`` /
    ``defined`` / ``invalid`` are excluded, so a plain ``restart --all-running``
    never wakes an agent the operator had deliberately stopped. No separate
    liveness rule is invented here. Order-preserving de-dup by name.
    """
    from .._helpers import get_agent_list_data
    from ..._state.registry import Registry

    seen: set[str] = set()
    names: list[str] = []
    for row in get_agent_list_data(Registry()):
        if row.get("status") != "running":
            continue
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
        if not dispatched:
            # No instances row → the SPEC's host pin routes (unregistered
            # pin raises UnknownSpecHostError into the outer except, loud).
            spec_peer = spec_host_fallback_peer(name, peers, verb="restart")
            if spec_peer is not None:
                _handler(spec_peer, {}, peers)
                dispatched = True
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
    "--all-running",
    "all_running",
    is_flag=True,
    default=False,
    help=(
        "Restart ONLY the agents that are currently RUNNING (live session). "
        "The least-surprising choice for 'restart the live fleet' — a "
        "deliberately-stopped agent stays stopped. Mutually exclusive with "
        "explicit NAME arguments and with --all-registry. Still requires "
        "-y/--yes."
    ),
)
@click.option(
    "--all-registry",
    "all_registry",
    is_flag=True,
    default=False,
    help=(
        "Restart EVERY agent 'sac agents list' shows — INCLUDING stopped "
        "ones. Mutually exclusive with explicit NAME arguments and with "
        "--all-running. Still requires -y/--yes."
    ),
)
@click.option(
    "--all",
    "all_alias",
    is_flag=True,
    default=False,
    help=(
        "Backward-compat alias for --all-registry (restarts stopped agents "
        "too). Prefer the explicit flags: --all-running restarts only the "
        "live fleet; --all-registry restarts every registered agent."
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
    all_running: bool,
    all_registry: bool,
    all_alias: bool,
    dry_run: bool,
    yes: bool,
    as_json: bool,
    fresh: bool,
) -> None:
    """Restart one or more agents.

    Pass one or more NAMEs, or a selection flag:

    \b
      --all-running   restart ONLY currently-running agents (live fleet)
      --all-registry  restart EVERY registered agent (INCLUDING stopped)
      --all           backward-compat alias for --all-registry

    For each name, the agent's recorded host is resolved first: a row on a
    remote peer is restarted over ssh on that peer (node-aware); otherwise
    the restart runs locally. Agents are restarted independently — one
    failing does not abort the rest, and the command exits non-zero if ANY
    restart failed.

    \b
    Example:
      $ sac agents restart foo -y
      $ sac agents restart foo bar baz -y      # several in one call
      $ sac agents restart --all-running -y    # only the live fleet
      $ sac agents restart --all-registry -y   # every registered agent
      $ sac agents restart foo --dry-run
      $ sac agents restart foo --json
    """
    # ``--all`` is a backward-compat alias for ``--all-registry`` (do not
    # break cron/callers that still pass the old flag). The remaining
    # selection modes are mutually exclusive with each other.
    registry_mode = all_registry or all_alias
    running_mode = all_running
    if registry_mode and running_mode:
        raise click.UsageError(
            "--all-running and --all-registry (--all) are mutually exclusive; "
            "pass exactly one selection flag."
        )
    batch_mode = registry_mode or running_mode

    if batch_mode and names:
        raise click.UsageError(
            "A selection flag (--all-running / --all-registry / --all) cannot "
            "be combined with explicit agent NAME arguments."
        )

    if batch_mode:
        targets = _enumerate_running() if running_mode else _enumerate_fleet()
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
            "Missing argument 'NAME...'. Pass one or more agent names, or a "
            "selection flag (--all-running / --all-registry / --all)."
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
        # multiple names or a batch selection flag emit a JSON array of
        # per-agent envelopes.
        if len(results) == 1 and not batch_mode:
            click.echo(_json.dumps(results[0]))
        else:
            click.echo(_json.dumps(results))

    if any_failed:
        sys.exit(1)


__all__ = ["restart"]
