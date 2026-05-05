"""``sac db`` noun group — SQLite-backed state inspection (F-CS11).

Subcommands:

  * ``sac db show`` — schema + per-table counts.
  * ``sac db query --table=<t>``  — list rows from a known table.
  * ``sac db migrate`` — one-shot import of the legacy
    ``registry/*.json`` shards into the ``instances`` table.

Read-only queries first; write paths (clean / tick / supervisor /
export / import) land in subsequent F-CS11 phases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .._state.state_db import (
    KNOWN_TABLES,
    import_legacy_registry,
    open_db,
    table_counts,
)
from ._helpers import _json_flag, console


@click.group(
    "db",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def db_group() -> None:
    """Inspect and maintain the sac state database (state.db).

    \b
    Examples:
      $ sac db show
      $ sac db query --table=instances --limit=20
      $ sac db migrate
    """


@db_group.command("show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def db_show(ctx: click.Context, as_json: bool) -> None:
    """Schema overview + per-table row counts."""
    counts = table_counts()
    payload = {"tables": counts, "known_tables": list(KNOWN_TABLES)}
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold]sac state.db[/bold]")
    for table in KNOWN_TABLES:
        n = counts.get(table, 0)
        console.print(f"  {table:<14}  {n:>6}")


@db_group.command("query")
@click.option(
    "--table",
    type=click.Choice(KNOWN_TABLES, case_sensitive=False),
    required=True,
    help="Table to read from.",
)
@click.option("--limit", type=int, default=20, help="Cap output rows (default 20).")
@click.option(
    "--where",
    type=str,
    default=None,
    help="Optional SQL fragment (no params; trusted operator input only).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def db_query(
    ctx: click.Context,
    table: str,
    limit: int,
    where: str | None,
    as_json: bool,
) -> None:
    """List rows from a known table, most recent first when possible."""
    sql = f"SELECT * FROM {table}"  # table is whitelisted via click.Choice
    if where:
        sql += f" WHERE {where}"
    # Order by a sensible default per table; no-op for tables without
    # a recognisable timestamp column.
    order_by = {
        "instances": "started_at DESC",
        "definitions": "first_seen_at DESC",
        "heartbeats": "ts DESC",
        "events": "ts DESC",
        "attempts": "ts DESC",
    }.get(table)
    if order_by:
        sql += f" ORDER BY {order_by}"
    sql += " LIMIT ?"

    with open_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]

    if _json_flag(ctx, as_json):
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print(f"[dim]({table}: no rows)[/dim]")
        return
    # Plain key-value rendering keeps the column set obvious without
    # rich.Table's width-management quirks on wide schemas.
    for i, row in enumerate(rows):
        console.print(f"[bold]row {i}[/bold]")
        for k, v in row.items():
            console.print(f"  {k}: {v}")


@db_group.command("migrate")
@click.option(
    "--registry-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Legacy registry directory (default: $SCITEX_AGENT_CONTAINER_REGISTRY_DIR "
    "else ~/.scitex/agent-container/registry).",
)
@click.option(
    "--host", type=str, default=None, help="Canonical hostname for imported rows."
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def db_migrate(
    ctx: click.Context,
    registry_dir: Path | None,
    host: str | None,
    as_json: bool,
) -> None:
    """One-shot import of legacy ``registry/*.json`` into ``instances``.

    Imported rows are marked ``exit_reason='reboot-swept'`` — they
    represent state captured before the SQLite migration and are
    not running by definition. Idempotent: re-running skips rows
    that already exist (matched by name + host + started_at).
    """
    if registry_dir is None:
        registry_dir = Path(
            os.environ.get(
                "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
                os.path.expanduser("~/.scitex/agent-container/registry"),
            )
        )
    result = import_legacy_registry(registry_dir, host=host)
    if _json_flag(ctx, as_json):
        click.echo(json.dumps({"registry_dir": str(registry_dir), **result}, indent=2))
        return
    console.print(
        f"Migrated from [cyan]{registry_dir}[/cyan]: "
        f"imported={result['imported']} skipped={result['skipped']}"
    )
