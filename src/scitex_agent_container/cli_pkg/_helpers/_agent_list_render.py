"""Agent-list presentation (JSON + rich-table).

Extracted from ``_agent_list.py`` (which had grown past the 512-line
per-file cap) so the DATA-ASSEMBLY layer (``get_agent_list_data`` and its
probe/label helpers) stays in ``_agent_list.py`` while the READER-facing
rendering lives here. ``_agent_list`` re-exports these names so existing
``from ._agent_list import print_agent_list`` importers are unchanged.
"""

from __future__ import annotations

import json as json_mod

import click
from rich.table import Table

from ..._state.registry import Registry
from ._console import console

__all__ = [
    "print_agent_list_json",
    "print_agent_list",
    "_is_ghost_row",
    "_extract_damaged_fields",
]


def print_agent_list_json(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print agent list as JSON."""
    from ._agent_list import get_agent_list_data

    data = get_agent_list_data(registry, capability=capability, machine=machine)
    click.echo(json_mod.dumps(data, indent=2))


def _is_ghost_row(row: dict) -> bool:
    """True for a dead registry entry: a LOCAL agent whose spec file is gone.

    Operator 2026-06-17: ``sac agents list`` should show only active agents
    by default. Stale ``instances`` rows (e.g. left by a pytest run whose
    tmp spec dir was cleaned) surface as ``status="unknown"`` with a
    ``"File not found"`` validation error — pure noise. They are hidden by
    default and revealed with ``--all``.

    Safety: only a LOCAL spec-missing row is a ghost. A REMOTE agent's spec
    lives on its own host (a local "File not found" says nothing about it),
    and a remote liveness-probe timeout also reports ``status="unknown"`` —
    neither must be hidden, or the fleet view would erase live peers. An
    on-disk spec that merely fails SCHEMA validation (``status="invalid"``)
    also stays visible so the operator can see and fix it.
    """
    host = row.get("host") or "local"
    if host not in ("local", ""):
        return False
    errors = row.get("validation_errors") or []
    return any("File not found" in str(e) for e in errors)


def print_agent_list(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    *,
    verbose: bool = False,
    show_all: bool = False,
) -> None:
    """Print a rich table of all registered agents.

    By default shows only active agents (dead spec-missing registry ghosts
    are hidden — see :func:`_is_ghost_row`) and omits the full spec ``Path``
    column (it folds every row to 10+ lines). ``show_all=True`` includes the
    ghosts; ``verbose=True`` adds the ``Path`` column back.
    """
    from ._agent_list import get_agent_list_data

    data = get_agent_list_data(registry, capability=capability, machine=machine)
    if not data:
        console.print("[dim]No agents found (registry empty, no specs on disk).[/dim]")
        return

    hidden = 0
    if not show_all:
        kept = [r for r in data if not _is_ghost_row(r)]
        hidden = len(data) - len(kept)
        data = kept
    if not data:
        console.print(
            "[dim]No active agents (all hidden as stale; --all to show).[/dim]"
        )
        return

    table = Table(title="Agents")
    # ``no_wrap`` keeps the agent name (the primary identifier) intact:
    # rich shrinks the fold-able Account/Path columns instead of
    # ellipsising the name when the terminal is narrow.
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Status")
    table.add_column("YAML")
    table.add_column("Host")
    # Account labels (e.g. ``<name> (<email>)``) can be long; fold within
    # the cell rather than stealing width from the name column.
    # Account folds the long ``<name> (<email>)`` label to ~5 lines; show the
    # short account name only by default (no_wrap), full label in --verbose.
    if verbose:
        table.add_column("Account", overflow="fold")
    else:
        table.add_column("Account", no_wrap=True, overflow="ellipsis")
    # ``Path`` (full spec.yaml path) folds every row to 10+ lines, so it is
    # verbose-only (operator 2026-06-17).
    if verbose:
        table.add_column("Path", overflow="fold")
    table.add_column("Started")
    cmap = {
        "running": "green",
        "stopped": "red",
        "defined": "yellow",
        "invalid": "bold red",
        "unknown": "dim",
    }
    for row in data:
        col = cmap.get(row["status"], "white")
        host = row.get("host") or "local"
        host_cell = host if host in ("local", "") else f"[cyan]{host}[/cyan]"
        errors = row.get("validation_errors") or []
        yaml_cell = (
            f"[bold red]✗ {', '.join(_extract_damaged_fields(errors)) or 'errors'}[/bold red]"
            if errors
            else "[green]✓[/green]"
        )
        started = row["started_at"] if row["started_at"] not in ("-", "?") else "—"
        account_cell = row.get("account") or "—"
        # Drop the ``(email)`` parenthetical in the default (compact) view so
        # the row stays one line; --verbose keeps the full ``name (email)``.
        if not verbose and " (" in account_cell:
            account_cell = account_cell.split(" (", 1)[0]
        cells = [
            row["name"],
            f"[{col}]{row['status']}[/{col}]",
            yaml_cell,
            host_cell,
            account_cell,
        ]
        if verbose:
            cells.append(row.get("path") or "—")
        cells.append(started)
        table.add_row(*cells)

    console.print(table)
    if hidden:
        console.print(
            f"[dim]({hidden} stale/ghost agent(s) hidden — --all to show, "
            "-v for paths)[/dim]"
        )
    # Full error text follows the table so the operator can copy-paste.
    for row in data:
        if row.get("validation_errors"):
            console.print(f"[bold red]✗ {row['name']}[/bold red] validation errors:")
            for err in row["validation_errors"]:
                console.print(f"    [red]- {err}[/red]")


def _extract_damaged_fields(errors: list[str]) -> list[str]:
    """Pull `spec.<field>` / top-level field names out of validator
    error strings so the YAML column can show *which* keys are broken
    without dumping the full error message into a narrow cell.
    """
    import re as _re

    fields: list[str] = []
    seen: set[str] = set()
    pat = _re.compile(r"(spec\.[a-zA-Z_][a-zA-Z_0-9.]*|metadata\.[a-zA-Z_]+)")
    for err in errors:
        for m in pat.findall(err):
            if m not in seen:
                seen.add(m)
                fields.append(m)
    # Cap the column width.
    if len(fields) > 4:
        fields = fields[:3] + [f"+{len(fields) - 3} more"]
    return fields
