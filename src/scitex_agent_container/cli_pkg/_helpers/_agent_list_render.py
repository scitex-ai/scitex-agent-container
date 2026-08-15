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
from .._account_list_format import format_dt_display_tz
from ._agent_list_auth import STATUS_AUTH_FAILED, is_live_status
from ._console import console

__all__ = [
    "print_agent_list_json",
    "print_agent_list",
    "_is_ghost_row",
    "_extract_damaged_fields",
]

# Status → colour. ``auth-failed`` is deliberately the loudest thing in the
# table: it is a LIVE agent that is accomplishing nothing, which is strictly
# worse than a stopped one (a stopped agent at least looks stopped).
_CMAP = {
    "running": "green",
    "auth-failed": "bold red",
    "stopped": "red",
    "defined": "yellow",
    "invalid": "bold red",
    "unknown": "dim",
}


def _fmt_age(seconds: int | None) -> str:
    """Compact age — ``45s`` / ``12m`` / ``6h`` / ``3d``. Empty when unknown."""
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _status_cell(row: dict) -> str:
    """The Status cell — auth-aware, and honest about how old its evidence is.

    An ``auth-failed`` row is the entire point of the auth cache: that agent is
    tmux-GREEN and doing NOTHING, because every API call it makes is rejected. It
    must not look like ``running``, and it must carry the AGE of the evidence —
    a verdict from 6 hours ago is a weaker claim than one from 60 seconds ago and
    may not be dressed up as the same thing::

        auth failed (2m ago)     bold red — a fresh check; act on it
        auth failed? (6h ago)    yellow, "?" — STALE cache; re-check before acting

    Every other status renders exactly as it always did.
    """
    status = row.get("status") or "unknown"
    if status != STATUS_AUTH_FAILED:
        col = _CMAP.get(status, "white")
        return f"[{col}]{status}[/{col}]"
    age = _fmt_age(row.get("auth_check_age_s")) or "age unknown"
    if row.get("auth_check_stale"):
        return f"[yellow]auth failed? ({age} ago)[/yellow]"
    return f"[bold red]auth failed ({age} ago)[/bold red]"


def _auth_cell(row: dict) -> str:
    """The verbose-only Auth cell: the raw cached verdict + its age.

    Shown for EVERY row, not just failing ones, because "when was this agent last
    checked at all?" is its own question. A column of ``never`` — or of ``6h`` —
    means the watchdog is not running, and therefore that every green row above
    is unverified rather than healthy. That is worth being able to see.
    """
    if not row.get("auth_checked_at"):
        return "[dim]never[/dim]"
    age = _fmt_age(row.get("auth_check_age_s"))
    stale = row.get("auth_check_stale")
    if row.get("auth_failed"):
        reason = row.get("auth_reason") or "unknown"
        remedy = row.get("auth_remedy") or "restart"
        body = f"failed {age} ({reason} → {remedy})"
        return f"[yellow]{body}?[/yellow]" if stale else f"[bold red]{body}[/bold red]"
    return f"[yellow]ok? {age}[/yellow]" if stale else f"[green]ok {age}[/green]"


def _print_auth_footer(data: list[dict]) -> None:
    """One line telling the operator what his green is actually worth.

    ``sac agents list`` reads a CACHE. If nothing has refreshed that cache, every
    ``running`` above means only "tmux is up" — the very ambiguity this feature
    exists to remove — so the absence of evidence has to be stated out loud
    rather than passed off silently as good news. Three cases:

    * never checked → the watchdog has never run here; green is unverified.
    * checked, but the freshest verdict is STALE → the watchdog has stopped;
      green is no longer verified.
    * fresh → say when, quietly, and name anyone who cannot authenticate.
    """
    from ..._state.auth_state import STALE_AFTER_S

    live = [r for r in data if is_live_status(r.get("status"))]
    if not live:
        return
    ages = [
        r["auth_check_age_s"]
        for r in live
        if r.get("auth_checked_at") and r.get("auth_check_age_s") is not None
    ]
    failed = [r for r in live if r.get("auth_failed")]
    hint = "run `sac agents auth-status` (or put it on a timer)"
    if not ages:
        console.print(
            f"[yellow]auth: never checked — a green agent is NOT verified "
            f"working, only tmux-alive; {hint}[/yellow]"
        )
        return
    freshest = min(ages)
    if freshest > STALE_AFTER_S:
        console.print(
            f"[yellow]auth: last checked {_fmt_age(freshest)} ago (STALE) — "
            f"green is no longer verified; {hint}[/yellow]"
        )
    else:
        unchecked = len(live) - len(ages)
        extra = f", {unchecked} unchecked" if unchecked else ""
        console.print(f"[dim]auth: checked {_fmt_age(freshest)} ago{extra}[/dim]")
    if failed:
        detail = ", ".join(
            f"{r['name']} ({r.get('auth_reason') or 'unknown'} → "
            f"{r.get('auth_remedy') or 'restart'})"
            for r in failed
        )
        console.print(
            f"[bold red]{len(failed)} agent(s) cannot authenticate: {detail}"
            "[/bold red]"
        )


def print_agent_list_json(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
) -> None:
    """Print agent list as JSON."""
    from ._agent_list import get_agent_list_data

    data = get_agent_list_data(
        registry, capability=capability, machine=machine, group=group
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
    registry: Registry | None,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    *,
    verbose: bool = False,
    show_all: bool = False,
    rows: list[dict] | None = None,
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

    ``rows`` renders a listing that was ALREADY assembled — the fleet-wide path,
    whose rows come from several hosts and therefore cannot be re-derived from
    one local ``registry``. It changes only WHERE the data came from: every
    filter, column and footer below behaves identically. The empty-listing text
    differs deliberately, because "registry empty, no specs on disk" is a claim
    about THIS machine and would be false about a fleet whose hosts answered.
    """
    from ._agent_list import get_agent_list_data

    if rows is not None:
        data = list(rows)
        empty_msg = "[dim]No agents on the host(s) that answered.[/dim]"
    else:
        # PERF: the default view discards non-running rows, so let the data
        # layer skip their account/movement enrichment. `-v`/`--all` show every
        # row, so they must stay fully enriched.
        data = get_agent_list_data(
            registry,
            capability=capability,
            machine=machine,
            group=group,
            running_only=not (verbose or show_all),
        )
        empty_msg = "[dim]No agents found (registry empty, no specs on disk).[/dim]"
    if not data:
        console.print(empty_msg)
        return

    # `--all` reveals dead spec-missing registry ghosts; hidden otherwise.
    hidden_ghosts = 0
    if not show_all:
        kept = [r for r in data if not _is_ghost_row(r)]
        hidden_ghosts = len(data) - len(kept)
        data = kept

    # DEFAULT view shows ONLY LIVE agents; `-v`/`--all` show the full roster.
    # Tally the hidden non-live rows by status for the footer.
    #
    # ``is_live_status`` — NOT ``== "running"``. An ``auth-failed`` agent IS
    # live (tmux up, pane process alive); it is simply not working. Filtering it
    # out as "not running" would hide the one row the operator most needs to see.
    show_full = verbose or show_all
    status_hidden: dict[str, int] = {}
    if not show_full:
        live = [r for r in data if is_live_status(r.get("status"))]
        for r in data:
            st = r.get("status") or "unknown"
            if not is_live_status(st):
                status_hidden[st] = status_hidden.get(st, 0) + 1
        data = live

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
    # ``Auth`` (the cached verdict + its age, for EVERY row) answers "is this
    # green verified, or merely tmux-alive?" — but one extra column on the
    # default view is a cost the compact view should not pay, so it lives behind
    # `-v`. The default view still shows a FAILING agent loudly (Status) and
    # summarises the cache's freshness in the footer.
    if verbose:
        table.add_column("Auth")
    # ``Path`` (full spec.yaml path) folds every row to 10+ lines, so it is
    # verbose-only (operator 2026-06-17).
    if verbose:
        table.add_column("Path", overflow="fold")
    table.add_column("Started")
    for row in data:
        # Host column: show the RESOLVED machine hostname (e.g. ``ywata-note-win``)
        # from ``host_display`` (set by get_agent_list_data), not the raw
        # ``"local"`` sentinel. Fall back to the raw host, then the sentinel.
        host = row.get("host_display") or row.get("host") or "local"
        host_cell = f"[cyan]{host}[/cyan]"
        errors = row.get("validation_errors") or []
        yaml_cell = (
            f"[bold red]✗ {', '.join(_extract_damaged_fields(errors)) or 'errors'}[/bold red]"
            if errors
            else "[green]✓[/green]"
        )
        # Started column: render the registry's raw ISO-8601 UTC stamp as a
        # pinned-tz ``YYYY-MM-DD HH:MM (JST)`` for readability (operator TG
        # 2026-07-13); the ``--json`` path keeps the raw ISO. Sentinels
        # ("-"/"?") stay an em-dash.
        raw_started = row["started_at"]
        if raw_started in ("-", "?"):
            started = "—"
        else:
            started = format_dt_display_tz(raw_started)
        account_cell = row.get("account") or "—"
        # Drop the ``(email)`` parenthetical in the default (compact) view so
        # the row stays one line; --verbose keeps the full ``name (email)``.
        if not verbose and " (" in account_cell:
            account_cell = account_cell.split(" (", 1)[0]
        cells = [
            row["name"],
            _status_cell(row),
            yaml_cell,
            host_cell,
            account_cell,
        ]
        if verbose:
            cells.append(_auth_cell(row))
        if verbose:
            cells.append(row.get("path") or "—")
        cells.append(started)
        table.add_row(*cells)

    console.print(table)

    # How much is the green above actually worth? Say it out loud — an unrefreshed
    # cache means every ``running`` here proves only that tmux is up.
    _print_auth_footer(data)

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
