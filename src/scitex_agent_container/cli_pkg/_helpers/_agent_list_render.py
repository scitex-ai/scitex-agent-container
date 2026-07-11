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
    tags: str | None = None,
) -> None:
    """Print agent list as JSON."""
    from ._agent_list import get_agent_list_data

    data = get_agent_list_data(
        registry, capability=capability, machine=machine, tags=tags
    )
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


def _print_hidden_footer(
    status_hidden: dict[str, int],
    hidden_ghosts: int,
    *,
    none_running: bool,
) -> None:
    """Print the default-view footer summarising the hidden non-running rows.

    ``status_hidden`` maps a status word (``stopped`` / ``invalid`` /
    ``defined`` / ``unknown``) to how many rows of it were hidden;
    ``hidden_ghosts`` is the count of dead spec-missing registry ghosts.
    ``defined`` is rendered as ``definitions`` (the template/definition rows
    the operator called out). Emits nothing when nothing was hidden.
    """
    # Canonical order + human labels; any residual status is appended as-is.
    order = [
        ("stopped", "stopped"),
        ("defined", "definitions"),
        ("invalid", "invalid"),
        ("unknown", "unknown"),
    ]
    known = {key for key, _ in order}
    parts: list[str] = []
    for key, word in order:
        n = status_hidden.get(key, 0)
        if n:
            parts.append(f"{n} {word}")
    for key, n in status_hidden.items():
        if key not in known and n:
            parts.append(f"{n} {key}")
    if hidden_ghosts:
        parts.append(f"{hidden_ghosts} stale")
    if not parts:
        return
    summary = ", ".join(parts)
    if none_running:
        console.print(
            f"[dim]No running agents ({summary} hidden — -v for all).[/dim]"
        )
    else:
        console.print(f"[dim]({summary} hidden — -v for all)[/dim]")


def print_agent_list(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    tags: str | None = None,
    *,
    verbose: bool = False,
    show_all: bool = False,
) -> None:
    """Print a rich table of registered agents.

    DEFAULT (no flags): show ONLY ``status="running"`` agents with their
    Account — the stopped / invalid / definition roster and the per-agent
    validation-error blocks are an unusable wall on a real fleet (operator
    TG 1490-1495). A one-line footer counts what was hidden.

    ``verbose=True`` (``-v``) restores the FULL list — every status
    (running/stopped/invalid/definition) PLUS the per-agent validation-error
    detail — and adds the spec ``Path`` column. ``show_all=True`` (``--all``)
    also shows the full list AND additionally includes dead spec-missing
    registry ghosts (see :func:`_is_ghost_row`), which stay hidden otherwise.
    """
    from ._agent_list import get_agent_list_data

    data = get_agent_list_data(
        registry, capability=capability, machine=machine, tags=tags
    )
    if not data:
        console.print("[dim]No agents found (registry empty, no specs on disk).[/dim]")
        return

    # `--all` reveals dead spec-missing registry ghosts; hidden otherwise.
    hidden_ghosts = 0
    if not show_all:
        kept = [r for r in data if not _is_ghost_row(r)]
        hidden_ghosts = len(data) - len(kept)
        data = kept

    # DEFAULT view shows ONLY running agents; `-v`/`--all` show the full
    # roster. Tally the hidden non-running rows by status for the footer.
    show_full = verbose or show_all
    status_hidden: dict[str, int] = {}
    if not show_full:
        running = [r for r in data if r.get("status") == "running"]
        for r in data:
            st = r.get("status") or "unknown"
            if st != "running":
                status_hidden[st] = status_hidden.get(st, 0) + 1
        data = running

    if not data:
        if show_full:
            console.print(
                "[dim]No active agents (all hidden as stale; --all to show).[/dim]"
            )
        else:
            _print_hidden_footer(status_hidden, hidden_ghosts, none_running=True)
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

    # Footer. DEFAULT view: summarise every hidden non-running row + ghosts.
    # FULL view (`-v`/`--all`): only note hidden ghosts when `-v` alone.
    if not show_full:
        _print_hidden_footer(status_hidden, hidden_ghosts, none_running=False)
    elif hidden_ghosts:
        console.print(
            f"[dim]({hidden_ghosts} stale/ghost agent(s) hidden — --all to "
            "show, -v for paths)[/dim]"
        )

    # Full per-agent validation-error text — FULL view only. In the default
    # view these blocks (repeated dozens of times on a real fleet) are the
    # wall the operator asked us to remove; they belong behind `-v`/`--all`.
    if show_full:
        for row in data:
            if row.get("validation_errors"):
                console.print(
                    f"[bold red]✗ {row['name']}[/bold red] validation errors:"
                )
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
