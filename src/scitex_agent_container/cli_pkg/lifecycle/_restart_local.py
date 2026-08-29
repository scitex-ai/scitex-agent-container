#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The LOCAL leg of ``sac agents restart`` — perform, verify, report.

Extracted from ``_restart.py`` (line cap) when the v4 step-5 beat
witness landed: this module owns the on-this-host restart —
cross-host ssh dispatch included — plus the postcondition check wiring
(:mod:`._restart_verify`) and the console rendering of its TERNARY
verdict. ``_restart.py`` stays the command/orchestration surface and
re-exports these names, so existing imports keep resolving.
"""

from __future__ import annotations

import json as _json
import time

import click

from ..._lifecycle._start_outcome import KIND_ALREADY_RUNNING, outcome_kind
from ..._lifecycle.lifecycle import agent_restart
from ..._state.host_config import load as _load_host_config
from ...config import load_config
from ...config._resolve import resolve_with_prefix
from .._helpers import console
from ._dispatch import try_dispatch_remote
from ._host_routing import spec_host_fallback_peer
from ._restart_remote import _dispatch_remote_restart, brokered_restart
from ._restart_verify import (
    read_beat_identity,
    read_run_identity,
    read_session_identity,
    verify_cycled,
)

__all__ = [
    "_NOT_CYCLED",
    "_print_local_outcome",
    "_refuse_fresh_on_bare_host",
    "_restart_locally",
    "_restart_via_broker",
]

# Named cause for a restart the postcondition check refuted. Distinct from
# KIND_ALREADY_RUNNING: that one is the START LEG telling us it no-op'd,
# this one is the AGENT ITSELF telling us its run never changed.
_NOT_CYCLED = "not-cycled"

# Bounded wait for the AFTER-restart beat witness (v4 step 5): the new
# runner adopts its incarnation on its first tick after the start path
# publishes the marker (default tick 10s), so a short poll converts an
# instant "cannot verify" into real runner-side testimony. Only paid on
# the fallback path — an agent whose tmux session answered never waits.
_BEAT_WITNESS_WAIT_S = 12.0


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


def _observe_run(name: str, *, min_ts: float | None = None, wait_s: float = 0.0):
    """One process-side observation, preferring tmux, falling back to the
    runner's own beat (v4 step 5).

    The tmux witness answers for TUI agents; an SDK agent has no session
    to ask about (``instances.screen`` NULL), which used to leave the
    verdict permanently at "cannot verify". The beat witness — the
    incarnation the runner process itself bound at boot — fills exactly
    that gap; ``min_ts``/``wait_s`` gate the AFTER-restart reading so a
    pre-restart beat can never impersonate the new run.
    """
    seen = read_session_identity(name)
    if seen.observed:
        return seen
    return read_beat_identity(name, min_ts=min_ts, wait_s=wait_s)


def _restart_locally(name: str, *, as_json: bool) -> tuple[dict, bool]:
    """Perform the restart on THIS host (ssh-dispatching to a peer if needed).

    Reached only when :func:`._restart_remote.must_broker_to_host` said
    this process can act. Human console output is printed here when ``not
    as_json``; JSON emission is left to the caller.
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
    #
    # BOTH witnesses are captured BEFORE the call, because both are
    # destroyed by it: ``agent_restart`` rewrites the marker and (when it
    # really works) replaces the tmux session. The process-side reading is
    # the one that makes this a check at all — the marker is written by
    # the start path we are checking, so on its own it can only ever agree
    # with itself (P0 2026-08-14, scitex-compute-04: "verified: ... is a
    # NEW run" printed over a tmux session alive and untouched since the
    # previous day). v4 step 5: where tmux has nothing to say (an SDK
    # agent), the runner's own incarnation-stamped beat is the second
    # witness — see ``_observe_run``.
    before = read_run_identity(name)
    session_before = _observe_run(name)
    restart_began = time.time()
    _result = agent_restart(name)
    restarted = _result is not False
    if outcome_kind(_result) == KIND_ALREADY_RUNNING:
        restarted = False
        no_op_reason = KIND_ALREADY_RUNNING
    verdict = verify_cycled(
        name,
        before,
        read_run_identity(name),
        session_before=session_before,
        session_after=_observe_run(
            name, min_ts=restart_began, wait_s=_BEAT_WITNESS_WAIT_S
        ),
    )
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
    """Console rendering for a locally-performed restart (no JSON here).

    The verdict is TERNARY, and each of its three states gets its own
    label. ``None`` used to be rendered as "NOT verified" — a binary
    label collapsing an ABSTENTION into an accusation; it now says
    CANNOT VERIFY, in the abstention's own words.
    """
    if restarted:
        console.print(f"[green]Agent '{name}' restarted[/green]")
        # Only a True verdict may be labelled "verified". A None verdict
        # is an ABSTENTION, and printing it under that word is how an
        # unchecked restart came to read as a checked one — while "NOT
        # verified" would accuse a restart nobody could observe.
        if verdict.verified:
            label = "verified"
        elif verdict.verified is None:
            label = "CANNOT VERIFY"
        else:  # pragma: no cover — a False verdict forces restarted=False upstream
            label = "NOT verified"
        console.print(f"[dim]{label}: {verdict.reason}[/dim]")
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
