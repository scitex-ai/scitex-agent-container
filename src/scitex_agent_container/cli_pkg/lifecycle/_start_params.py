#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``--params-file`` template fan-out for ``sac agents start``.

Extracted from :mod:`._start` to keep the click entry point inside the
per-file 512-line cap. F-CS2: ``--params-file`` expands a template +
CSV into N materialised yamls; the resulting paths replace ``targets``
so downstream code (preflight, singleton check, JSON report) treats
them identically. Pure helper — it validates, calls the production
``expand_params_file``, and returns the rewritten target tuple.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .._helpers import console


def classify_targets(
    targets: tuple[str, ...], *, iter_agent_yamls
) -> tuple[list[str], list[str]]:
    """Split targets into ``(single_targets, bulk_yamls_from_dirs)``.

    Directory targets expand to all ``<name>/<name>.yaml`` under them
    (via ``iter_agent_yamls``); non-directory targets are paths or agent
    names. Extracted from ``_start.py`` to keep the click entry under
    the per-file line cap.
    """
    single_targets: list[str] = []
    bulk_yamls_from_dirs: list[str] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            for _name, yp in iter_agent_yamls(p):
                bulk_yamls_from_dirs.append(yp)
        else:
            single_targets.append(t)
    return single_targets, bulk_yamls_from_dirs


def resolve_session_shorthand(
    *,
    continue_session: bool,
    fresh_session: bool,
    session_mode: str | None,
) -> str | None:
    """Fold the ``--continue`` / ``--fresh`` shorthand into ``session_mode``.

    The shorthands are mutually exclusive with each other and may not
    contradict an explicit ``--session`` (precedence: CLI > spec >
    role-default > global default fresh). Exits (``sys.exit(2)``) on a
    conflict, exactly like the inline block did; otherwise returns the
    resolved ``session_mode``.
    """
    if continue_session and fresh_session:
        click.echo("Error: --continue and --fresh are mutually exclusive.", err=True)
        sys.exit(2)
    shorthand = "continue" if continue_session else ("fresh" if fresh_session else None)
    if shorthand is None:
        return session_mode
    if session_mode is not None and session_mode.lower() != shorthand:
        click.echo(
            f"Error: --{shorthand} contradicts --session {session_mode}; "
            "pass only one.",
            err=True,
        )
        sys.exit(2)
    return shorthand


def expand_params_targets(
    targets: tuple[str, ...],
    *,
    params_file: Path,
    params_out: Path | None,
    params_overwrite: bool,
    as_json: bool,
) -> tuple[str, ...]:
    """Expand the single template TARGET into N materialised yaml paths.

    Exits (``sys.exit(2)``) on the validation failures the inline block
    raised: not exactly one TARGET, a missing template, or an
    ``expand_params_file`` ValueError / FileExistsError. Returns the new
    ``targets`` tuple on success.
    """
    if len(targets) != 1:
        click.echo(
            "Error: --params-file requires exactly one TARGET (the "
            "template yaml). Got "
            f"{len(targets)} targets.",
            err=True,
        )
        sys.exit(2)
    template_path = Path(targets[0]).expanduser()
    if not template_path.is_file():
        click.echo(
            f"Error: --params-file template not found: {template_path}",
            err=True,
        )
        sys.exit(2)
    out_dir = (params_out or Path("params-fleet-out")).expanduser()
    from ..._state.fleet_template import expand_params_file

    try:
        materialised = expand_params_file(
            template_path,
            params_file,
            out_dir,
            overwrite=params_overwrite,
        )
    except (ValueError, FileExistsError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    if not as_json:
        console.print(
            f"[bold]--params-file[/bold]  expanded "
            f"{len(materialised)} agent(s) under [cyan]{out_dir}[/cyan]"
        )
    return tuple(str(p) for p in materialised)


__all__ = [
    "classify_targets",
    "expand_params_targets",
    "resolve_session_shorthand",
]
