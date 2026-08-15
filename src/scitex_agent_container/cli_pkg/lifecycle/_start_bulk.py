#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bulk-directory branch of ``sac agents start``.

Extracted from :mod:`._start` to keep the click entry point inside
the per-file 512-line cap. The behaviour is unchanged: walk every
``<name>/<name>.yaml`` in the discovered directory targets, skip
singletons that don't run on this host, and call ``agent_start`` on
each — printing per-agent OK / SKIP / FAILED rows. Errors inside the
loop do NOT abort the bulk run (a single bad spec must not block the
remaining 49 agents).
"""

from __future__ import annotations

from typing import Callable

import click

from ..._lifecycle.lifecycle import agent_start
from ...config import load_config
from ...config._host import resolve_hostname
from .._helpers import console
from ._common import _local_host_names, _singleton_skip_reason


def run_bulk_path(
    yamls: list[str],
    *,
    yes: bool,
    no_preflight: bool,
    force: bool,
    dry_run: bool,
    preflight_runner: Callable[[], None],
) -> None:
    """Drive the bulk-directory branch of ``sac agents start``.

    Args:
        yamls: Discovered ``<name>/<name>.yaml`` paths.
        yes: ``--yes/-y`` flag (required for bulk).
        no_preflight: Pass-through to ``agent_start``.
        force: Pass-through to ``agent_start``.
        dry_run: Pass-through to ``agent_start``.
        preflight_runner: Idempotent OAuth preflight runner from the
            click entry; injected so the bulk and single paths share
            the "once per invocation" gating.

    Raises:
        SystemExit: When ``yes`` is False (exit code 2 — bulk requires
            explicit confirmation).
    """
    if not yes:
        click.echo(
            f"Refusing to start {len(yamls)} agents without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)
    preflight_runner()
    # stx-allow: fallback (reason: runtime state error — handled gracefully)
    try:
        current_host = resolve_hostname()
    except RuntimeError:
        current_host = ""
    console.print(f"=== [blue]Starting {len(yamls)} agents...[/blue] ===")
    for yaml_path in yamls:
        # stx-allow: fallback (reason: one agent's config parse or
        # launch failure must not abort the remaining agents in a bulk
        # start; printing FAILED and continuing is the correct
        # bulk-safe behavior)
        try:
            config = load_config(yaml_path)
            skip = _singleton_skip_reason(
                config, current_host, local_names=_local_host_names(current_host)
            )
            if skip:
                console.print(f"  [yellow]SKIP[/yellow] {config.name}: {skip}")
                continue
            # ``config.remote`` was deleted in WI-6. Host pinning under
            # v3 lives in ``spec.host`` / ``spec.hosts`` and is shown
            # via ``sac host`` / ``sac agent status``, not in the
            # bulk-start one-liner.
            console.print(
                f"  [blue]{config.name}[/blue]...",
                end=" ",
            )
            agent_start(
                yaml_path,
                no_preflight=no_preflight,
                force=force,
                dry_run=dry_run,
            )
            console.print(
                "[green]DRY-RUN OK[/green]" if dry_run else "[green]OK[/green]"
            )
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            console.print(f"[red]FAILED: {exc}[/red]")


__all__ = ["run_bulk_path"]
