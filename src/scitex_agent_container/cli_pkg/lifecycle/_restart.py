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
import sys

import click

from ..._lifecycle._start_outcome import KIND_ALREADY_RUNNING, outcome_kind
from ..._lifecycle.lifecycle import agent_restart
from ..._state.host_config import load as _load_host_config
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._dispatch import try_dispatch_remote
from ._host_routing import spec_host_fallback_peer

# Cross-host dispatch + host-listen bypass live in ``_restart_remote``.
# Re-exported here so existing imports (tests included) keep resolving.
from ._restart_remote import (  # noqa: F401
    _bypass_base_url_available,
    _dispatch_remote_restart,
    _restart_via_host_bypass,
    _should_try_host_bypass,
)
from ._selection import (
    _enumerate_fleet,
    _enumerate_running,
    bulk_selection_options,
    resolve_selection,
)


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
    # Set when the start leg no-op'd over a live agent instead of cycling it;
    # surfaced as ``reason``/``hint`` on the envelope so a FAILED restart is
    # diagnosable without reading stderr.
    no_op_reason: str | None = None
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
            # Trust the PEER'S OWN verdict, not the mere fact that ssh exited
            # 0. The peer runs `sac agents restart --json`, whose envelope
            # carries its `restarted` flag; ssh returning 0 only proves the
            # command RAN, not that the restart WORKED. Defaulting to True
            # here would relocate the same false-success one hop away. A peer
            # too old to report the flag omits it — treat a missing flag as
            # UNKNOWN-but-not-asserted-true rather than inventing a success.
            peer_verdict = envelope_holder.get("restarted")
            remote_ok = peer_verdict is not False
            out = {
                "name": name,
                "restarted": remote_ok,
                "host": envelope_holder.get("_peer"),
                "a2a_port": envelope_holder.get("a2a_port"),
                "dispatched": True,
            }
            if not as_json:
                if remote_ok:
                    console.print(
                        f"[green]Agent '{name}' restarted on "
                        f"'{envelope_holder.get('_peer')}'[/green]"
                    )
                else:
                    console.print(
                        f"[red]Agent '{name}' NOT restarted on "
                        f"'{envelope_holder.get('_peer')}' — the peer reported "
                        f"the start leg failed.[/red]"
                    )
            return out, remote_ok

        # Local restart (row on this host, or no row — spec fallback).
        # When that LOCAL resolution fails (no registry row AND no
        # resolvable spec) AND we are inside a container with the host
        # listen reachable (SAC_LISTEN_BASE_URL injected), broker the
        # restart to the HOST listen — exactly like the spawn bypass.
        try:
            # ``agent_restart`` returns the START leg's result. DISCARDING it
            # (as this did) makes a FAILED restart print green "restarted":
            # stop leaves the old session alive, start hits the
            # duplicate-session guard and FAILs, and the very next line claims
            # success. The operator hit exactly this on neurovista — he
            # believed it had relaunched on freshly-picked credentials, was in
            # fact still talking to the OLD process holding the OLD token, saw
            # "Login expired", and went hunting a credential store that was
            # perfectly healthy. A tool that reports a failure as a pass sends
            # its user to the wrong subsystem.
            # `is not False`, NOT bool(): only an EXPLICIT False is a failure.
            # `agent_start` returns True on its own paths but forwards
            # `runtime.start(...)` on another, and a runtime that returns None
            # must NOT be read as "the restart failed" — inventing a false
            # FAILURE is just the mirror of the false SUCCESS we are fixing,
            # and would be equally misleading.
            #
            # A restart's contract is that the process CYCLED. The start
            # leg's idempotent "already running -> no-op" branch satisfies
            # `is not False` while having launched NOTHING, so it must be
            # reported as a FAILED restart (incident 2026-07-12,
            # scitex-storage: the API answered `{"restarted": true}` over
            # an agent whose pid never changed, and a caller counting rc=0
            # marked an unrestarted agent as rolled). `outcome_kind`
            # returns None for a plain True/False/None, so this is safe on
            # any older or hand-rolled start result.
            _result = agent_restart(name)
            restarted = _result is not False
            if outcome_kind(_result) == KIND_ALREADY_RUNNING:
                restarted = False
                no_op_reason = KIND_ALREADY_RUNNING
        except RuntimeError as exc:
            if not _should_try_host_bypass(exc):
                raise
            envelope = _restart_via_host_bypass(name)
            brokered = envelope.get("returncode") == 0
            out = {
                "name": name,
                "restarted": brokered,
                "dispatched": False,
                "via": "host-listen",
                "host_response": envelope,
            }
            if not as_json:
                if brokered:
                    console.print(
                        f"[green]Agent '{name}' restarted via host listen[/green]"
                    )
                else:
                    console.print(
                        f"[red]Agent '{name}' NOT restarted via host listen "
                        f"(returncode={envelope.get('returncode')})[/red]"
                    )
                console.print(_json.dumps(envelope))
            return out, brokered
        out = {"name": name, "restarted": restarted, "dispatched": False}
        if no_op_reason is not None:
            # Name the no-op explicitly. Without this the envelope is
            # `{"restarted": false}` with no cause, which reads as the
            # generic start-leg failure below and sends the operator to
            # the wrong recovery (kill-session + --fresh) for an agent
            # that is in fact perfectly healthy and simply never cycled.
            out["reason"] = no_op_reason
            out["hint"] = (
                f"the agent was already running and the start leg no-op'd, "
                f"so NOTHING was restarted — it is still the OLD process on "
                f"its OLD credentials. Force the cycle with: "
                f"sac agents start {name} -y --force"
            )
        if not as_json:
            if restarted:
                console.print(f"[green]Agent '{name}' restarted[/green]")
            elif no_op_reason is not None:
                console.print(
                    f"[red]Agent '{name}' NOT restarted — it was already "
                    f"running and the start leg no-op'd, so nothing cycled. "
                    f"It is still the OLD process on its OLD credentials."
                    f"[/red]"
                )
                console.print(
                    f"[yellow]Force the cycle with:\n"
                    f"  sac agents start {name} -y --force[/yellow]"
                )
            else:
                console.print(
                    f"[red]Agent '{name}' NOT restarted — the stop ran but the "
                    f"START leg failed. The agent is either DOWN, or still the "
                    f"OLD process on its OLD credentials.[/red]"
                )
                console.print(
                    f"[yellow]Most common cause: the previous session ignored "
                    f"SIGTERM, so start hit the duplicate-session guard.\n"
                    f"Recover with:\n"
                    f"  tmux kill-session -t tui-{name}\n"
                    f"  sac agents start {name} -y --fresh[/yellow]"
                )
        return out, restarted
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
@bulk_selection_options("restart")
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
    # Selection semantics (flags, mutual exclusion, enumeration) are SHARED
    # with ``sac agents stop`` — see ``_selection.resolve_selection``. The
    # enumerators are passed in so this module keeps its own swappable seam.
    targets, batch_mode = resolve_selection(
        names,
        all_running=all_running,
        all_registry=all_registry,
        all_alias=all_alias,
        enumerate_running=_enumerate_running,
        enumerate_fleet=_enumerate_fleet,
    )
    if batch_mode and not targets:
        if as_json:
            click.echo(_json.dumps([]))
        else:
            console.print("[dim]No agents found to restart.[/dim]")
        return

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
