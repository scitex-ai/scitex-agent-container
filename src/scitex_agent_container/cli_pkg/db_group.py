"""``sac db`` noun group — SQLite-backed state inspection (F-CS11).

Subcommands:

  * ``sac db show`` — schema + per-table counts.
  * ``sac db query --table=<t>``  — list rows from a known table.
  * ``sac db migrate`` — one-shot import of the legacy
    ``registry/*.json`` shards into the ``instances`` table.

Read-only queries first; write paths (clean / tick / supervisor /
export / import) land in subsequent F-CS11 phases.

``export`` and ``import`` moved to :mod:`._db_wire_cmds` on 2026-08-28
(per-file line cap); they are registered on this group at the bottom of
this module and re-exported from it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .._state import state_db as _state_db
from .._state.state_db import (
    KNOWN_TABLES,
    gc_dead_instances,
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
    """Schema overview + per-table row counts.

    The output NAMES THE DATABASE IT READ (``store``).
    ``SCITEX_AGENT_CONTAINER_STATE_DB`` is set per-agent in every sac
    container, so an agent reads its OWN shard — which never holds
    fleet rows — while the populated registry sits elsewhere. Without
    the path, all-zero counts look exactly like a wiped fleet registry:
    on 2026-08-09 THREE agents independently reached that conclusion from
    their own empty shard (two escalating P1 data loss) while the host DB
    was healthy — three, independently, is what makes it a tool defect
    rather than a coincidence.

    \b
    Example:
      $ sac db show
      $ sac db show --json
    """
    # Classify the store BEFORE counting. `table_counts()` goes through
    # `open_db`, which calls `init_schema` unconditionally — so on a wrong or
    # zero-byte path it CREATES empty tables and then truthfully reports zero
    # rows for all of them. Measured 2026-08-09: that is how a 0-byte
    # state.db produced a confident "no agents registered" while twelve
    # agents were running. Counting first and asking questions later is what
    # made the failure invisible.
    from .._state.state_db import DEFAULT_DB_PATH
    from .._state.state_db_health import inspect_store

    store = inspect_store(DEFAULT_DB_PATH)
    counts = table_counts()
    # Read through the MODULE, not a from-import: the constant is bound
    # at import time from the env, so a captured copy goes stale the
    # moment anything re-resolves it. Reporting a stale path would be
    # the very bug this line exists to prevent.
    payload = {
        "store": str(_state_db.DEFAULT_DB_PATH),
        "tables": counts,
        "known_tables": list(KNOWN_TABLES),
        # Three-valued, at the reporting boundary: a zero here means "zero
        # rows" ONLY when store_state is "populated".
        "store_state": store.state,
        "store_path": str(store.path),
        "counts_are_authoritative": store.is_populated,
    }
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold]sac state.db[/bold]")
    console.print(f"  [dim]store: {_state_db.DEFAULT_DB_PATH}[/dim]")
    if not store.is_populated:
        console.print(f"  [yellow]{store.describe()}[/yellow]")
        console.print(
            "  [yellow]the counts below are NOT evidence about the fleet[/yellow]"
        )
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
    """List rows from a known table, most recent first when possible.

    \b
    Example:
      $ sac db query --table=instances --limit=20
      $ sac db query --table=channel_events --where="delivered_at IS NULL" --json
    """
    sql = f"SELECT * FROM {table}"  # table is whitelisted via click.Choice
    if where:
        sql += f" WHERE {where}"
    # Order by a sensible default per table; no-op for tables without
    # a recognisable timestamp column.
    order_by = {
        "instances": "started_at DESC",
        # ``definitions`` (``first_seen_at DESC``), ``instance_heartbeats``
        # and ``events`` (``ts DESC`` each) had entries here until
        # 2026-08-28. All three left KNOWN_TABLES that day, so ``--table``
        # can no longer name them and these keys could only ever be dead.
        # ``attempts`` had a ``ts DESC`` entry here until 2026-08-28. It
        # left KNOWN_TABLES, so ``--table`` can no longer name it and this
        # key could only ever be dead.
    }.get(table)
    if order_by:
        sql += f" ORDER BY {order_by}"
    sql += " LIMIT ?"

    with open_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]

    if _json_flag(ctx, as_json):
        # Bare ARRAY, and NOTHING else on this path — not even stderr.
        # Wrapping breaks consumers that index it (a published contract
        # is a MIGRATION). A stderr line is worse than it looks: the MCP
        # wrapper reads Click's `result.output`, which MERGES stderr into
        # stdout, so one extra line makes the JSON unparseable and agents
        # get `data: None` instead of rows. Measured, not assumed.
        # An MCP caller gets the store from the wrapper (_mcp/_tools/_db)
        # as a sibling key, where it cannot corrupt the payload.
        click.echo(json.dumps(rows, indent=2))
        return
    console.print(f"[dim]store: {_state_db.DEFAULT_DB_PATH}[/dim]")
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
    "else ~/.scitex/agent-container/runtime/registry).",
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

    \b
    Example:
      $ sac db migrate
      $ sac db migrate --registry-dir ~/.scitex/agent-container/runtime/registry
      $ sac db migrate --host head-nas --json
    """
    if registry_dir is None:
        from .._runtime_paths import runtime_base_dir

        registry_dir = Path(
            os.environ.get(
                "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
                str(runtime_base_dir() / "registry"),
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
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be swept without mutating state.db.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirm prompt (currently always implicit; reserved).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def db_clean(
    ctx: click.Context,
    heartbeat_stale_seconds: int,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Sweep dead instances. Replaces ``sac registry clean``.

    Three checks (see _state.state_db.gc_dead_instances):
      1. Boot-epoch — rows started before /proc/stat btime
         are marked ``reboot-swept``.
      2. PID liveness — local rows whose pid no longer exists
         are marked ``crashed``.
      3. Heartbeat staleness — rows whose last_heartbeat_at is
         older than ``--heartbeat-stale-seconds`` are marked
         ``gc-stale``.

    \b
    Example:
      $ sac db clean
      $ sac db clean --dry-run
      $ sac db clean --heartbeat-stale-seconds 600 --json
    """
    del yes  # reserved; no prompt today
    counters = gc_dead_instances(
        heartbeat_stale_seconds=heartbeat_stale_seconds,
        dry_run=dry_run,
    )
    if _json_flag(ctx, as_json):
        payload = {"dry_run": dry_run, **counters}
        click.echo(json.dumps(payload, indent=2))
        return
    total = sum(counters.values())
    label = "would-sweep" if dry_run else "swept"
    console.print(f"[bold]sac db clean[/bold]  {label}={total}")
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

    \b
    Example:
      $ sac db tick
      $ sac db tick --heartbeat-stale-seconds 600
    """
    del ctx
    gc_dead_instances(heartbeat_stale_seconds=heartbeat_stale_seconds)


# The cross-host wire pair lives in :mod:`._db_wire_cmds` — imported at the
# BOTTOM so ``db_group`` is already defined when the registration below
# runs, and re-exported so every existing
# ``from ...cli_pkg.db_group import db_export`` import site keeps resolving.
from ._db_wire_cmds import db_export, db_import  # noqa: E402

db_group.add_command(db_export)
db_group.add_command(db_import)

__all__ = [
    "db_clean",
    "db_export",
    "db_group",
    "db_import",
    "db_migrate",
    "db_query",
    "db_show",
    "db_tick",
]
