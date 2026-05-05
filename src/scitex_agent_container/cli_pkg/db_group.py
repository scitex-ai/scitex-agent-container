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
    export_state,
    gc_dead_instances,
    import_legacy_registry,
    import_state,
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


@db_group.command("clean")
@click.option(
    "--heartbeat-stale-seconds",
    type=int,
    default=300,
    help="Mark instance gc-stale when last_heartbeat_at is older than this.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def db_clean(ctx: click.Context, heartbeat_stale_seconds: int, as_json: bool) -> None:
    """Sweep dead instances. Replaces ``sac registry clean``.

    Three checks (see _state.state_db.gc_dead_instances):
      1. Boot-epoch — rows started before /proc/stat btime
         are marked ``reboot-swept``.
      2. PID liveness — local rows whose pid no longer exists
         are marked ``crashed``.
      3. Heartbeat staleness — rows whose last_heartbeat_at is
         older than ``--heartbeat-stale-seconds`` are marked
         ``gc-stale``.
    """
    counters = gc_dead_instances(heartbeat_stale_seconds=heartbeat_stale_seconds)
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(counters, indent=2))
        return
    total = sum(counters.values())
    console.print(f"[bold]sac db clean[/bold]  swept={total}")
    for kind, n in counters.items():
        if n:
            console.print(f"  {kind:<14}  {n}")


@db_group.command("tick")
@click.option(
    "--heartbeat-stale-seconds",
    type=int,
    default=300,
)
@click.pass_context
def db_tick(ctx: click.Context, heartbeat_stale_seconds: int) -> None:
    """One-shot housekeeping pass. Designed for cron / systemd timers.

    Silent on success (just exits 0); writes only to state.db. Same
    semantics as ``sac db clean`` but produces no human output, and
    the exit code is the only signal:

      * 0 — pass completed (zero or more rows swept).
      * non-zero — sweep raised; the operator should investigate.
    """
    del ctx
    gc_dead_instances(heartbeat_stale_seconds=heartbeat_stale_seconds)


@db_group.command("export")
@click.option(
    "--since",
    "since",
    type=str,
    default=None,
    help="ISO-8601 timestamp; emit only rows newer than this. Omit for full dump.",
)
@click.option(
    "--output",
    "output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write JSON to this path; default stdout.",
)
@click.option(
    "--host",
    type=str,
    default=None,
    help="Stamp this canonical host into the dump header.",
)
def db_export(since: str | None, output: Path | None, host: str | None) -> None:
    """Dump state.db rows as a JSON delta. Consumed by orochi.

    Default emits to stdout so it can be piped over ssh:

    \b
      ssh peer sac db export --since "$last_seen" \\
        | sac db import -

    With ``--output FILE`` writes to FILE instead. The dump is
    self-describing: includes ``schema``, ``exported_at``, ``since``,
    ``host``, and per-table row arrays.
    """
    payload = export_state(since=since, host=host)
    blob = json.dumps(payload, indent=2)
    if output is None:
        click.echo(blob)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(blob)


@db_group.command("import")
@click.argument(
    "input_path",
    type=click.Path(dir_okay=False, exists=False, path_type=Path),
    required=True,
)
@click.option("--json", "as_json", is_flag=True, help="Output report as JSON.")
@click.pass_context
def db_import(ctx: click.Context, input_path: Path, as_json: bool) -> None:
    """Ingest a JSON dump produced by ``sac db export``.

    Pass ``-`` to read from stdin (the canonical orochi-pull pattern).
    Idempotent: rows already present (matched by primary key) are
    silently skipped.
    """
    if str(input_path) == "-":
        blob = click.get_text_stream("stdin").read()
    else:
        blob = input_path.read_text()
    payload = json.loads(blob)
    inserted = import_state(payload)
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "source": str(input_path),
                    "host": payload.get("host"),
                    "since": payload.get("since"),
                    "inserted": inserted,
                },
                indent=2,
            )
        )
        return
    total = sum(inserted.values())
    src = payload.get("host", "?")
    console.print(
        f"[bold]sac db import[/bold]  from=[cyan]{src}[/cyan]  inserted={total}"
    )
    for table, n in inserted.items():
        if n:
            console.print(f"  {table:<14}  {n}")
