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

from ..._lifecycle._start_outcome import KIND_ALREADY_RUNNING, outcome_kind
from ..._lifecycle.lifecycle import agent_restart
from ..._state.host_config import load as _load_host_config
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console
from ._dispatch import try_dispatch_remote
from ._host_routing import spec_host_fallback_peer

# Cross-host dispatch + the host-listen broker live in ``_restart_remote``;
# the postcondition check lives in ``_restart_verify``. Re-exported here so
# existing imports (tests included) keep resolving.
from ._restart_remote import (  # noqa: F401
    _dispatch_remote_restart,
    _restart_via_host_bypass,
    brokered_restart,
    log_restart_decision,
    must_broker_to_host,
)
from ._restart_verify import read_run_identity, verify_cycled  # noqa: F401
from ._selection import (
    _enumerate_fleet,
    _enumerate_running,
    bulk_selection_options,
    resolve_selection,
)

# Named cause for a restart the postcondition check refuted. Distinct from
# KIND_ALREADY_RUNNING: that one is the START LEG telling us it no-op'd,
# this one is the AGENT ITSELF telling us its run never changed.
_NOT_CYCLED = "not-cycled"


def _refuse_fresh_on_bare_host(name: str, *, as_json: bool) -> tuple[dict, bool]:
    """``--fresh`` outside a container: fail loud with the direct command.

    A fresh (no-resume) restart is implemented as ``start --force
    --fresh`` on the HOST, so there is nothing to broker to when we are
    already on the host. Silently downgrading to a resuming restart would
    re-wedge exactly the agent this flag exists to recover.
    """
    msg = (
        f"--fresh restart requires the host broker (run inside a "
        f"container). On a bare host run: sac agents start {name} "
        f"--force --fresh"
    )
    if not as_json:
        click.echo(msg, err=True)
    return {"name": name, "error": msg, "fresh": True}, False


def _restart_locally(name: str, *, as_json: bool) -> tuple[dict, bool]:
    """Perform the restart on THIS host (ssh-dispatching to a peer if needed).

    Reached only when :func:`must_broker_to_host` said this process can
    act. Human console output is printed here when ``not as_json``; JSON
    emission is left to the caller.
    """
    # Set when the start leg no-op'd over a live agent instead of cycling
    # it, or when the postcondition check refuted the cycle; surfaced as
    # ``reason``/``hint`` so a FAILED restart is diagnosable without
    # reading stderr.
    no_op_reason: str | None = None

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
        # pin raises UnknownSpecHostError into the caller's except, loud).
        spec_peer = spec_host_fallback_peer(name, peers, verb="restart")
        if spec_peer is not None:
            _handler(spec_peer, {}, peers)
            dispatched = True
    if dispatched:
        # Trust the PEER'S OWN verdict, not the mere fact that ssh exited
        # 0. The peer runs `sac agents restart --json`, whose envelope
        # carries its `restarted` flag (and, since the postcondition check
        # landed, its `verified` field too); ssh returning 0 only proves
        # the command RAN, not that the restart WORKED. Defaulting to True
        # here would relocate the same false-success one hop away. A peer
        # too old to report the flag omits it — treat a missing flag as
        # UNKNOWN-but-not-asserted-true rather than inventing a success.
        # The postcondition is NOT re-derived here: the agent's runtime
        # dir lives on the PEER, so a local probe would read either
        # nothing or, worse, a same-named local agent's marker.
        peer_verdict = envelope_holder.get("restarted")
        remote_ok = peer_verdict is not False
        out = {
            "name": name,
            "restarted": remote_ok,
            "host": envelope_holder.get("_peer"),
            "a2a_port": envelope_holder.get("a2a_port"),
            "dispatched": True,
            "verified": envelope_holder.get("verified"),
            "verified_reason": envelope_holder.get("verified_reason"),
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

    # ---- local restart -----------------------------------------------
    # ``agent_restart`` returns the START leg's result. DISCARDING it (as
    # this once did) makes a FAILED restart print green "restarted": stop
    # leaves the old session alive, start hits the duplicate-session guard
    # and FAILs, and the very next line claims success. The operator hit
    # exactly this on neurovista — he believed it had relaunched on freshly
    # picked credentials, was in fact still talking to the OLD process
    # holding the OLD token, saw "Login expired", and went hunting a
    # credential store that was perfectly healthy. A tool that reports a
    # failure as a pass sends its user to the wrong subsystem.
    #
    # `is not False`, NOT bool(): only an EXPLICIT False is a failure.
    # `agent_start` returns True on its own paths but forwards
    # `runtime.start(...)` on another, and a runtime that returns None must
    # NOT be read as "the restart failed" — inventing a false FAILURE is
    # just the mirror of the false SUCCESS we are fixing.
    #
    # A restart's contract is that the process CYCLED. The start leg's
    # idempotent "already running -> no-op" branch satisfies `is not False`
    # while having launched NOTHING, so it must be reported as a FAILED
    # restart (incident 2026-07-12, scitex-storage). `outcome_kind` returns
    # None for a plain True/False/None, so this is safe on any older or
    # hand-rolled start result.
    #
    # Both of those read the RETURN VALUE of the call. The check below
    # reads the AGENT: a restart that changed nothing must not report
    # success no matter what the call returned (P0 2026-07-20 — an
    # in-container restart printed green over an untouched process).
    before = read_run_identity(name)
    _result = agent_restart(name)
    restarted = _result is not False
    if outcome_kind(_result) == KIND_ALREADY_RUNNING:
        restarted = False
        no_op_reason = KIND_ALREADY_RUNNING
    verdict = verify_cycled(name, before, read_run_identity(name))
    if verdict.verified is False:
        # DEFINITIVE: we held the before-evidence and the run did not
        # change (or vanished). Veto the success. ``verified is None``
        # (no evidence either way) deliberately changes nothing.
        restarted = False
        no_op_reason = no_op_reason or _NOT_CYCLED

    out = {"name": name, "restarted": restarted, "dispatched": False}
    out.update(verdict.as_dict())
    if no_op_reason is not None:
        # Name the no-op explicitly. Without this the envelope is
        # `{"restarted": false}` with no cause, which reads as the generic
        # start-leg failure below and sends the operator to the wrong
        # recovery (kill-session + --fresh) for an agent that is in fact
        # perfectly healthy and simply never cycled.
        out["reason"] = no_op_reason
        out["hint"] = (
            f"the agent did not cycle, so NOTHING was restarted — it is "
            f"still the OLD process on its OLD credentials. Force the "
            f"cycle with: sac agents start {name} -y --force"
        )
    if not as_json:
        _print_local_outcome(name, restarted, no_op_reason, verdict)
    return out, restarted


def _print_local_outcome(name, restarted, no_op_reason, verdict) -> None:
    """Console rendering for a locally-performed restart (no JSON here)."""
    if restarted:
        console.print(f"[green]Agent '{name}' restarted[/green]")
        console.print(f"[dim]verified: {verdict.reason}[/dim]")
        return
    if no_op_reason == _NOT_CYCLED:
        console.print(f"[red]Agent '{name}' NOT restarted — {verdict.reason}[/red]")
        console.print(
            f"[yellow]Force the cycle with:\n"
            f"  sac agents start {name} -y --force[/yellow]"
        )
        return
    if no_op_reason is not None:
        console.print(
            f"[red]Agent '{name}' NOT restarted — it was already "
            f"running and the start leg no-op'd, so nothing cycled. "
            f"It is still the OLD process on its OLD credentials.[/red]"
        )
        console.print(
            f"[yellow]Force the cycle with:\n"
            f"  sac agents start {name} -y --force[/yellow]"
        )
        return
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


def _restart_via_broker(name: str, *, as_json: bool, fresh: bool) -> tuple[dict, bool]:
    """Hand the whole restart to the host listen and report ITS verdict."""
    out, ok = brokered_restart(name, fresh=fresh)
    if not as_json:
        verb = "fresh-restarted" if fresh else "restarted"
        if out.get("scheduled"):
            console.print(f"[yellow]Agent '{name}' restart SCHEDULED on host[/yellow]")
            console.print(f"[dim]{out.get('verified_reason')}[/dim]")
        elif ok:
            console.print(f"[green]Agent '{name}' {verb} via host listen[/green]")
            console.print(f"[dim]verified: {out.get('verified_reason')}[/dim]")
        else:
            console.print(
                f"[red]Agent '{name}' NOT {verb} via host listen "
                f"(returncode="
                f"{out.get('host_response', {}).get('returncode')})[/red]"
            )
            console.print(_json.dumps(out.get("host_response")))
    return out, ok


def _restart_one(name: str, *, as_json: bool, fresh: bool) -> tuple[dict, bool]:
    """Restart ONE agent; return ``(json_envelope, ok)``.

    ONE decision, made explicitly and logged BEFORE any work: can this
    process perform the restart, or must it be brokered to the host? The
    plain and ``--fresh`` paths ask the same question, so ``--fresh``
    cannot silently take a route the plain path is locked out of (and
    vice versa — which is how the plain path spent its life reporting
    success for restarts it never performed).

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
        if broker:
            out, ok = _restart_via_broker(name, as_json=as_json, fresh=fresh)
        elif fresh:
            out, ok = _refuse_fresh_on_bare_host(name, as_json=as_json)
        else:
            out, ok = _restart_locally(name, as_json=as_json)
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
