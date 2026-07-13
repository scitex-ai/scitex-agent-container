#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded-parallel multi-start launcher for ``sac agents start``.

When ``sac agents start`` is handed MULTIPLE targets (or a bulk
directory of ``<name>/<name>.yaml`` agents), launching them all in one
in-process loop serialises the work and a naive in-process thread pool
would RACE: ``agent_start`` writes a shared SQLite state DB and
allocates ports, so two threads stepping through it concurrently
corrupt each other's port claims / instance rows.

The race-safe shape (mirroring how ``sac listen`` already spawns
agents — process isolation, see ``_listen/_agent_exec.py``) is to
dispatch each target as ITS OWN

    sac agents start <target> --yes --no-redispatch [propagated flags]

subprocess. Each child owns its own process, opens its own SQLite
connection, and the kernel serialises the DB writes — no shared
in-process mutable state to race. We bound the fan-out with a
``ThreadPoolExecutor(max_workers=concurrency)`` (each worker just
blocks on ``subprocess.run``) and sleep ``stagger`` seconds between
submissions so N agents don't all hit the port allocator in the same
millisecond.

A SINGLE target never reaches here — the click entry keeps the
unchanged in-process path for the one-target case (byte-identical
behaviour). This module is only engaged for the genuine multi-target
fan-out, and only when no per-target-interactive flag (foreground /
one-shot / resume / dry-run / params-file) is in play.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import click

from ..._sac_binary import sac_binary as _sac_binary
from .._helpers import console


@dataclass
class _Result:
    """One child ``sac agents start`` subprocess outcome."""

    target: str
    returncode: int
    stdout: str
    stderr: str


def build_child_argv(
    target: str,
    *,
    sac_bin: str,
    no_preflight: bool,
    force: bool,
    session_mode: str | None,
    strict_drift: bool,
    broker_self: bool,
) -> list[str]:
    """Build the child ``sac agents start <target> ...`` argv.

    ``--yes`` (the child is operator-pre-approved by the parent's own
    confirmation) and ``--no-redispatch`` (the parent already owns the
    redispatch decision; the child must not re-enter the cross-host
    routing branch) are always present. Pass-through flags are only
    appended when set so the child's defaults match a hand-typed
    single-target invocation.
    """
    argv = [sac_bin, "agents", "start", target, "--yes", "--no-redispatch"]
    if no_preflight:
        argv.append("--no-preflight")
    if force:
        argv.append("--force")
    if session_mode:
        argv += ["--session", session_mode]
    if strict_drift:
        argv.append("--strict-drift")
    if broker_self:
        argv.append("--broker-self")
    return argv


def run_parallel_targets(
    targets: list[str],
    *,
    concurrency: int,
    stagger: float,
    no_preflight: bool,
    force: bool,
    session_mode: str | None,
    strict_drift: bool,
    broker_self: bool,
) -> None:
    """Launch ``targets`` as bounded-parallel child subprocesses.

    Each target becomes its own ``sac agents start <target> --yes
    --no-redispatch ...`` process, capped at ``concurrency`` in flight
    and ``stagger`` seconds apart at submission. Per-agent rc + a short
    output tail are summarised; the call exits non-zero (``sys.exit(1)``)
    if ANY child failed.

    ``concurrency`` is clamped to ``>= 1`` and ``stagger`` to ``>= 0``
    so pathological flag values can't deadlock or busy-loop.
    """
    sac_bin = _sac_binary()
    workers = max(1, int(concurrency))
    pause = max(0.0, float(stagger))

    console.print(
        f"=== [blue]Starting {len(targets)} agents[/blue] "
        f"[dim](concurrency={workers}, stagger={pause:g}s)[/dim] ==="
    )

    def _launch(target: str) -> _Result:
        proc = subprocess.run(
            build_child_argv(
                target,
                sac_bin=sac_bin,
                no_preflight=no_preflight,
                force=force,
                session_mode=session_mode,
                strict_drift=strict_drift,
                broker_self=broker_self,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        return _Result(
            target=target,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )

    results: list[_Result] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for idx, target in enumerate(targets):
            if idx > 0 and pause > 0.0:
                time.sleep(pause)
            futures.append(pool.submit(_launch, target))
        for fut in futures:
            results.append(fut.result())

    any_error = False
    for res in results:
        if res.returncode == 0:
            console.print(f"  [green]OK[/green] {res.target}")
        else:
            any_error = True
            tail = (res.stderr or res.stdout or "").strip().splitlines()
            hint = tail[-1] if tail else f"rc={res.returncode}"
            console.print(
                f"  [red]FAILED[/red] {res.target} "
                f"[dim](rc={res.returncode}: {hint})[/dim]"
            )

    if any_error:
        sys.exit(1)


def maybe_run_parallel(
    *,
    single_targets: list[str],
    bulk_yamls: list[str],
    concurrency: int,
    stagger: float,
    yes: bool,
    no_preflight: bool,
    force: bool,
    session_mode: str | None,
    strict_drift: bool,
    broker_self: bool,
    foreground: bool,
    multi_foreground: bool,
    one_shot: bool,
    resume_id: str | None,
    dry_run: bool,
    as_json: bool,
    preflight_runner: Callable[[], None],
) -> bool:
    """Route a MULTI-target launch through :func:`run_parallel_targets`.

    Returns True iff it handled the launch (the caller then returns
    immediately). Returns False — leaving the in-process single/bulk
    loops to run — for the SINGLE-target case and for every
    per-target-interactive / report mode (foreground, one-shot, resume,
    dry-run, JSON report), whose semantics the in-process paths own.

    A bulk-directory multi-launch still requires ``--yes`` (exit 2
    without it), matching the in-process bulk path's confirmation gate.
    """
    all_targets = list(bulk_yamls) + list(single_targets)
    parallel_safe = not (
        foreground or multi_foreground or one_shot or resume_id or dry_run or as_json
    )
    if len(all_targets) <= 1 or not parallel_safe:
        return False
    if bulk_yamls and not yes:
        click.echo(
            f"Refusing to start {len(all_targets)} agents without --yes/-y.",
            err=True,
        )
        sys.exit(2)
    preflight_runner()
    run_parallel_targets(
        all_targets,
        concurrency=concurrency,
        stagger=stagger,
        no_preflight=no_preflight,
        force=force,
        session_mode=session_mode,
        strict_drift=strict_drift,
        broker_self=broker_self,
    )
    return True


__all__ = ["run_parallel_targets", "build_child_argv", "maybe_run_parallel"]
