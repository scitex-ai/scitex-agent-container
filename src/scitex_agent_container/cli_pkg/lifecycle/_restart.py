#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents restart`` — node-aware stop-then-start of agent(s).

Accepts ONE OR MORE agent names plus a selection flag: ``--all-running``
(only the live fleet), ``--all-registry`` (every registered agent), and
``--all`` (back-compat alias for ``--all-registry``). Each name restarts
independently; the command exits non-zero if ANY restart failed.

WHERE a restart runs is decided ONCE, up front, by
:func:`._restart_remote.must_broker_to_host` — "am I inside an apptainer
SIF, where an agent's process/tmux/runtime-dir are all out of reach?"
Both the plain and the ``--fresh`` path ask that one question, and the
answer is written to the decision log before any work starts. Inside a
SIF the whole restart is brokered to the host's ``sac listen``; outside
one, this process performs it.

On the bare host the restart is node-aware: an active
``state.db.instances`` row with ``host != current_host`` routes over ssh
to that peer (``sac agents restart <name> --yes --json`` on the node that
runs the agent — see ``_dispatch.try_dispatch_remote``). When NO row
exists at all, the SPEC's ``host:`` pin routes instead
(``_host_routing.spec_host_fallback_peer`` — transparent remote routing,
operator directive 2026-07-10); a pin naming an UNREGISTERED host fails
loud with the registered-peer list. Otherwise it delegates to
:func:`._lifecycle.lifecycle.agent_restart`, which resolves the spec from
the registry row OR the standard discovery chain, so a pre-autorecord
agent restarts instead of hard-failing with "not found in registry".

Every locally-performed restart then VERIFIES ITS OWN POSTCONDITION
against the agent's ``instance_id`` marker (:mod:`._restart_verify`)
before it is allowed to report success — ``rc=0`` means "the call
returned", never "the state changed".
"""

from __future__ import annotations

import json as _json
import sys

import click

from .._helpers import agent_name_complete, console

# The LOCAL leg (perform + verify + render) lives in ``_restart_local``;
# cross-host dispatch + the host-listen broker live in ``_restart_remote``;
# the postcondition check lives in ``_restart_verify``. Re-exported here so
# existing imports (tests included) keep resolving.
from ._restart_local import (  # noqa: F401
    _NOT_CYCLED,
    _print_local_outcome,
    _refuse_fresh_on_bare_host,
    _restart_locally,
    _restart_via_broker,
)
from ._restart_remote import (  # noqa: F401
    _dispatch_remote_restart,
    _restart_via_host_bypass,
    brokered_restart,
    log_restart_decision,
    must_broker_to_host,
)
from ._restart_verify import (  # noqa: F401
    read_beat_identity,
    read_run_identity,
    read_session_identity,
    verify_cycled,
)
from ._selection import (
    _enumerate_fleet,
    _enumerate_running,
    bulk_selection_options,
    resolve_selection,
)


def _restart_one(
    name: str, *, as_json: bool, fresh: bool, engine: str | None = None
) -> tuple[dict, bool]:
    """Restart ONE agent; return ``(json_envelope, ok)``.

    ONE decision, made explicitly and logged BEFORE any work: can this
    process perform the restart, or must it be brokered to the host? The
    plain and ``--fresh`` paths ask the same question, so ``--fresh``
    cannot silently take a route the plain path is locked out of (and
    vice versa — which is how the plain path spent its life reporting
    success for restarts it never performed).

    ``engine`` (``--engine <key>``) reaches only the LOCAL leg. The two
    other routes carry no engine field in the argv / request body they
    build — the host-listen broker's POST and the peer's ssh
    ``sac agents restart`` — so an engine handed to them would be
    DROPPED and the agent would come back on its default backend. That
    is the silent fallback this axis exists to refuse, so each route
    fails loud instead, naming the command that works there.

    Never raises for an ordinary restart fault and never calls
    ``sys.exit`` — the caller aggregates the batch exit code.
    """
    broker = must_broker_to_host()
    site = "host-listen" if broker else "local"
    log_restart_decision(
        event="decided",
        agent=name,
        site=site,
        fresh=fresh,
        why=(
            "inside an apptainer SIF: the agent's process, tmux session and "
            "runtime dir all live on the bare host, so the restart is "
            "brokered to `sac listen`"
            if broker
            else "not inside an apptainer SIF: this process can perform the "
            "restart itself (ssh-dispatching to a peer if the agent's row "
            "says it runs there)"
        ),
    )
    # stx-allow: fallback (reason: config resolution, cross-host ssh dispatch,
    # the host broker and agent_restart can all raise; an error envelope is
    # cleaner than an unhandled traceback, and the failure is still reported
    # as a FAILED restart — never swallowed into a success)
    try:
        if broker and engine:
            msg = (
                f"--engine {engine!r} cannot be honoured from inside a "
                "container: the restart is brokered to the host's `sac "
                "listen`, whose request body has no engine field, so the "
                "engine would be silently dropped and the agent would "
                "restart on its DEFAULT engine. Run on the host: sac "
                f"agents start {name} --force --yes --engine {engine}"
            )
            if not as_json:
                console.print(f"[red]{msg}[/red]")
            out, ok = {"name": name, "error": msg, "restarted": False}, False
        elif broker:
            out, ok = _restart_via_broker(name, as_json=as_json, fresh=fresh)
        elif fresh:
            out, ok = _refuse_fresh_on_bare_host(name, as_json=as_json)
        else:
            out, ok = _restart_locally(name, as_json=as_json, engine=engine)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if not as_json:
            console.print(f"[red]Error: {exc}[/red]")
        out, ok = {"name": name, "error": str(exc)}, False
    log_restart_decision(
        event="completed",
        agent=name,
        site=site,
        fresh=fresh,
        ok=ok,
        verified=out.get("verified"),
        verified_reason=out.get("verified_reason"),
        error=out.get("error"),
    )
    return out, ok


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
@click.option(
    "--engine",
    "engine",
    type=str,
    default=None,
    metavar="KEY",
    help=(
        "Restart on the named engine from the spec's `engines:` block "
        "(e.g. --engine qwen38-27b) instead of its declared default. START "
        "TIME ONLY — the choice applies to this restart's start leg and "
        "never rebinds mid-session. An unknown key, or an engine that "
        "cannot be honoured (unregistered provider, incomplete inline "
        "provider, unset auth env var, unknown harness), FAILS the restart "
        "naming what could not be honoured; sac never falls back to "
        "another engine. Reachability is not probed unless "
        "SAC_ENGINE_PROBE=1. Single-agent, local-leg only: a batch "
        "selection, or an agent that lives on a peer or must be brokered "
        "to the host, fails loud with the command to run there."
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
    engine: str | None,
) -> None:
    """Restart one or more agents.

    Pass one or more NAMEs, or a selection flag:

    \b
      --all-running   restart ONLY currently-running agents (live fleet)
      --all-registry  restart EVERY registered agent (INCLUDING stopped)
      --all           backward-compat alias for --all-registry

    Inside a container the restart is brokered to the host's ``sac
    listen`` (an in-SIF process cannot touch a host agent's tmux session).
    On the host, the agent's recorded node is resolved first: a row on a
    remote peer is restarted over ssh on that peer; otherwise the restart
    runs here and is verified against the agent's own instance marker.
    Agents are restarted independently — one failing does not abort the
    rest, and the command exits non-zero if ANY restart failed.

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

    # --engine names ONE engine key, and keys are declared per spec, so
    # one key does not name the same backend across agents. Applying it
    # to a batch would start some agents on a backend their spec never
    # declared. Fail loud rather than pick per agent.
    if engine and len(targets) > 1:
        click.echo(
            f"Error: --engine {engine} restarts ONE agent — engine keys are "
            "declared per spec, so one key does not name the same backend "
            f"across the {len(targets)} selected agents. Restart each "
            "separately.",
            err=True,
        )
        raise SystemExit(2)

    if dry_run:
        for name in targets:
            click.echo(
                f"[dry-run] would restart agent '{name}'"
                + (f" on engine '{engine}'" if engine else "")
            )
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
        envelope, ok = _restart_one(
            name, as_json=as_json, fresh=fresh, engine=engine
        )
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
