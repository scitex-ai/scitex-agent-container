"""``sac db`` noun group — instance-registry maintenance.

Subcommands:

  * ``sac db migrate`` — one-shot import of the legacy
    ``registry/*.json`` shards into ``instances``.
  * ``sac db clean`` — sweep dead instance rows.
  * ``sac db tick`` — the same sweep, silent, for cron / systemd timers.

``show`` and ``query`` were DELETED on 2026-08-29 along with the rest of
the SQLite read surface. Both went through ``open_db``, and
``KNOWN_TABLES`` was already empty, so ``--table`` could not parse any
value and ``show`` could only ever count nothing. ``export`` and
``import`` went the same day, from ``_db_wire_cmds``, which was deleted
with them: they were one JSON wire format over those same tables, and
the shared PostgreSQL store is the sync now — there is no peer left to
ship a delta to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .._state.state_db import gc_dead_instances, import_legacy_registry
from ._helpers import _json_flag, console


@click.group(
    "db",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def db_group() -> None:
    """Maintain the sac instance registry.

    \b
    Examples:
      $ sac db clean
      $ sac db migrate
    """


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
    help="Report what would be swept without mutating the registry.",
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

    Silent on success (just exits 0). Same semantics as ``sac db
    clean`` but produces no human output, and the exit code is the
    only signal:

      * 0 — pass completed (zero or more rows swept).
      * non-zero — sweep raised; the operator should investigate.

    \b
    Example:
      $ sac db tick
      $ sac db tick --heartbeat-stale-seconds 600
    """
    del ctx
    gc_dead_instances(heartbeat_stale_seconds=heartbeat_stale_seconds)


__all__ = [
    "db_clean",
    "db_group",
    "db_migrate",
    "db_tick",
]
