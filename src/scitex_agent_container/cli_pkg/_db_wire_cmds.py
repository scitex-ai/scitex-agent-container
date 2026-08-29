"""``sac db export`` / ``sac db import`` — the CROSS-HOST WIRE.

Extracted from :mod:`.db_group` on 2026-08-28, when that module went past
the 512-line per-file cap. The cut is along a real seam rather than at a
convenient line number: the other five ``sac db`` commands (``show`` /
``query`` / ``migrate`` / ``clean`` / ``tick``) read or repair THIS host's
state.db and print it, while these two are one JSON wire format with one
schema-version contract, and they only ever change together.

Both are plain :func:`click.command` functions; :mod:`.db_group` imports
them, registers them with ``db_group.add_command`` and re-exports the
names, so every existing ``from ...db_group import db_export`` import site
resolves unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .._state.state_db import (
    KNOWN_TABLES,
    export_state,
    import_state,
)
from ._helpers import _json_flag, console


@click.command("export")
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
@click.option(
    "--tables",
    "tables_csv",
    type=str,
    default=None,
    help=(
        "Comma-separated subset of KNOWN_TABLES to include in the dump "
        "(non-listed tables emit as empty arrays). Unknown names fail "
        "loud at parse time — including `comms_nodes`, which left "
        "KNOWN_TABLES on 2026-08-28 when the directory moved to the "
        "shared store."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute the dump and print row counts only — no JSON / file write.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the (currently never-shown) confirm prompt; reserved for parity.",
)
def db_export(
    since: str | None,
    output: Path | None,
    host: str | None,
    tables_csv: str | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Dump state.db rows as a JSON delta. Consumed by an external aggregator.

    Default emits to stdout so it can be piped over ssh:

    \b
      ssh peer sac db export --since "$last_seen" \\
        | sac db import -

    With ``--output FILE`` writes to FILE instead. The dump is
    self-describing: includes ``schema``, ``exported_at``, ``since``,
    ``host``, and per-table row arrays.

    \b
    Example:
      $ sac db export
      $ sac db export --since 2026-05-01T00:00:00Z --output dump.json
      $ sac db export --tables instances,lineage
      $ sac db export --dry-run
    """
    del yes  # reserved
    tables: list[str] | None = None
    if tables_csv is not None:
        tables = [t.strip() for t in tables_csv.split(",") if t.strip()]
        unknown = [t for t in tables if t not in KNOWN_TABLES]
        if unknown:
            raise click.BadParameter(
                f"unknown table(s) {unknown!r}; valid names are {list(KNOWN_TABLES)}",
                param_hint="--tables",
            )
    payload = export_state(since=since, host=host, tables=tables)
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "host": payload.get("host"),
                    "since": payload.get("since"),
                    "row_counts": {
                        k: len(v) for k, v in payload.get("tables", {}).items()
                    },
                },
                indent=2,
            )
        )
        return
    blob = json.dumps(payload, indent=2)
    if output is None:
        click.echo(blob)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(blob)


@click.command("import")
@click.argument(
    "input_path",
    type=click.Path(dir_okay=False, exists=False, path_type=Path),
    required=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Parse the dump and print would-insert counts; do NOT write to state.db.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the (currently never-shown) confirm prompt; reserved for parity.",
)
@click.option("--json", "as_json", is_flag=True, help="Output report as JSON.")
@click.pass_context
def db_import(
    ctx: click.Context,
    input_path: Path,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Ingest a JSON dump produced by ``sac db export``.

    Pass ``-`` to read from stdin (the canonical aggregator-pull pattern).
    Idempotent: rows already present (matched by primary key) are
    silently skipped.

    \b
    Example:
      $ sac db import dump.json
      $ ssh peer sac db export | sac db import -
      $ sac db import dump.json --dry-run --json
    """
    del yes  # reserved
    if str(input_path) == "-":
        blob = click.get_text_stream("stdin").read()
    else:
        blob = input_path.read_text()
    payload = json.loads(blob)
    if dry_run:
        would_insert = {
            table: len(rows) for table, rows in payload.get("tables", {}).items()
        }
        if _json_flag(ctx, as_json):
            click.echo(
                json.dumps(
                    {
                        "source": str(input_path),
                        "host": payload.get("host"),
                        "since": payload.get("since"),
                        "dry_run": True,
                        "would_insert": would_insert,
                    },
                    indent=2,
                )
            )
            return
        total = sum(would_insert.values())
        src = payload.get("host", "?")
        console.print(
            f"[bold]sac db import[/bold] (dry-run)  from=[cyan]{src}[/cyan]  "
            f"would-insert={total}"
        )
        for table, n in would_insert.items():
            if n:
                console.print(f"  {table:<14}  {n}")
        return
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
