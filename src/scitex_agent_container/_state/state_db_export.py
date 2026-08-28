"""Cross-host export / import + legacy registry import for state.db.

Extracted from :mod:`state_db` to keep that module under the project
line cap. Public symbols are re-exported from :mod:`state_db` so
existing ``from .state_db import export_state`` imports keep working.

Each host writes locally; an external aggregator (a separate concern)
pulls deltas via ssh and aggregates them. sac never reaches out —
orchestrator-agnostic by design.

  ssh <peer> sac db export --since <ts> --format json

Wire format::

  {
    "schema": 1,
    "exported_at": "<iso>",
    "since": "<iso>" | null,
    "host": "<canonical>",   # the host that produced the dump
    "tables": {
      "definitions": [ {row}, ... ],
      "attempts":    [ {row}, ... ],
      ...
    }
  }

Filtering: each table picks a sensible "advance" column and emits
only rows where that column >= since (or all rows when since is None).
``definitions`` and ``incarnations`` emit when *either* their
start/seen timestamp OR end timestamp is >= since — an aggregator needs
both halves of the lifecycle.

``instances`` and ``events`` used to be the headline tables here. They
moved to per-host PostgreSQL on 2026-08-28 and left ``KNOWN_TABLES``,
so this ssh-and-JSON path no longer carries them; the store replicates
them through its own federation instead.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .._env import getenv as _sac_env

EXPORT_SCHEMA_VERSION = 1


def _table_filter_clauses(
    since: str | None,
    known_tables: tuple[str, ...],
) -> dict[str, tuple[str, tuple]]:
    """Per-table SQL fragments + params for ``--since`` filtering.

    Returns ``{table: (sql_fragment, params)}``. Empty fragment means
    "no filter; emit everything". Tables not explicitly named fall
    back to a ``ts >= ?`` filter; tables with no ``ts``-shaped column
    must be added to the explicit map below.
    """
    if since is None:
        return {t: ("", ()) for t in known_tables}
    explicit = {
        "definitions": ("WHERE first_seen_at >= ?", (since,)),
        # ``instances`` and ``events`` had entries here until 2026-08-28.
        # Both moved to per-host PostgreSQL (state_db_instances /
        # state_db_instance_events) and left KNOWN_TABLES, so neither
        # mapping can be selected again — and a WHERE clause naming a
        # table SQLite no longer has reads as "sac still exports this".
        # Their cross-host replication is now the store's own federation,
        # not this ssh-and-JSON path.
        "instance_heartbeats": ("WHERE ts >= ?", (since,)),
        "heartbeats": ("WHERE ts >= ?", (since,)),
        "attempts": ("WHERE ts >= ?", (since,)),
        "turns": ("WHERE ts >= ?", (since,)),
        "errors": ("WHERE ts >= ?", (since,)),
        # WI-2 ACL tables — ``created_at`` is the row-mint time.
        "node_tokens": ("WHERE created_at >= ?", (since,)),
        "lineage": ("WHERE created_at >= ?", (since,)),
        "comms_grants": ("WHERE created_at >= ?", (since,)),
        # ADR-0014 — anti-entropy filter advances on ``updated_at`` so
        # a tombstoned row (``ended_at`` set) still ships on the next
        # pull until both sides converge.
        "comms_nodes": ("WHERE updated_at >= ?", (since,)),
        # Phase-3 ACL table (ADR-0010 Step 2). Uses ``updated_at`` since
        # the row is upserted on every agent_start with no historical
        # tail (latest write wins).
        "node_comms_policy": ("WHERE updated_at >= ?", (since,)),
        # acl_deny_notify_log's entry lived here until 2026-08-20. The table
        # moved to per-host PostgreSQL and left KNOWN_TABLES, so this mapping
        # could never be selected again — and a WHERE clause naming a table
        # SQLite no longer has reads as "sac still exports this".
        # v4 step 5 — birth certificates. A row moves when it is BORN or
        # when its death is mirrored on, so filter on either stamp (same
        # shape as ``instances``).
        "incarnations": (
            "WHERE born_at >= ? OR exited_at >= ?",
            (since, since),
        ),
    }
    return {t: explicit.get(t, ("WHERE ts >= ?", (since,))) for t in known_tables}


def export_state(
    since: str | None = None,
    db_path: Path | None = None,
    host: str | None = None,
    tables: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Dump the registry tables (and ``attempts``) into a JSON-able dict.

    Used by ``sac db export``; an aggregator consumes the result via
    ``sac db import`` (or its own importer).

    ``tables`` (added 2026-05 alongside ADR-0014's anti-entropy sync)
    optionally restricts the dump to a subset of :data:`KNOWN_TABLES`.
    Tables NOT listed are emitted as empty arrays so the wire shape
    stays stable for :func:`import_state` (which iterates over
    ``KNOWN_TABLES``). Raises ``ValueError`` on an unknown table name
    — caller (``sac db export --tables ...``) maps that to a
    ``click.BadParameter`` so operator typos surface at parse time.
    """
    from .state_db import KNOWN_TABLES, _resolve_host, now_iso, open_db

    canonical_host = _resolve_host(host)
    if tables is None:
        selected = tuple(KNOWN_TABLES)
    else:
        selected = tuple(tables)
        unknown = [t for t in selected if t not in KNOWN_TABLES]
        if unknown:
            raise ValueError(
                f"export_state: unknown table(s) {unknown!r}; "
                f"valid names are {list(KNOWN_TABLES)}"
            )
    filters = _table_filter_clauses(since, KNOWN_TABLES)
    out: dict[str, list[dict]] = {}
    with open_db(db_path) as conn:
        for table in KNOWN_TABLES:
            if table not in selected:
                out[table] = []
                continue
            where, params = filters[table]
            sql = f"SELECT * FROM {table} {where}".strip()
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            out[table] = rows
    return {
        "schema": EXPORT_SCHEMA_VERSION,
        "exported_at": now_iso(),
        "since": since,
        "host": canonical_host,
        "tables": out,
    }


def import_state(payload: dict, db_path: Path | None = None) -> dict[str, int]:
    """Ingest a dict produced by :func:`export_state`.

    Idempotent: rows are inserted with ``OR IGNORE`` on their PK.
    Returns ``{table: rows_inserted}``.
    """
    from .state_db import KNOWN_TABLES, open_db

    if not isinstance(payload, dict) or "tables" not in payload:
        raise ValueError("import_state: payload missing 'tables' key")
    schema = payload.get("schema")
    if schema != EXPORT_SCHEMA_VERSION:
        raise ValueError(
            f"import_state: unsupported schema version {schema!r} "
            f"(expected {EXPORT_SCHEMA_VERSION})"
        )
    tables = payload["tables"]
    inserted: dict[str, int] = {t: 0 for t in KNOWN_TABLES}
    with open_db(db_path) as conn:
        for table in KNOWN_TABLES:
            rows = tables.get(table, [])
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(cols)
            sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
            for row in rows:
                cur = conn.execute(sql, tuple(row.get(c) for c in cols))
                inserted[table] += cur.rowcount
    return inserted


def import_legacy_registry(
    registry_dir: Path,
    db_path: Path | None = None,
    host: str | None = None,
) -> dict[str, int]:
    """Lift the JSON files under ``registry_dir`` into ``instances``.

    Each JSON shard becomes one ``instances`` record marked
    ``exit_reason='reboot-swept'`` with ``ended_at`` = now. Idempotent:
    existing records matched by ``(name, host, started_at)`` are skipped.

    Returns ``{"imported": N, "skipped": M}``.

    ``db_path`` is accepted and IGNORED since 2026-08-28: ``instances``
    moved to PostgreSQL and there is no file to point at. The parameter
    stays in the signature because this is a public re-export with
    callers in the CLI and in ``db tick``; removing it is a separate,
    caller-visible change from moving the storage.

    The duplicate check is now a scan of the records rather than an
    indexed SELECT. That is the honest trade at this size — the legacy
    registry is a directory of per-agent JSON shards on one host, tens of
    files — and it keeps the natural key ``(name, host, started_at)``
    doing the deduplication instead of inventing a surrogate that would
    not survive a second store boundary.
    """
    from .state_db import new_uuid7, now_iso
    from .state_db_instances import all_instances, put_instance_record

    if host is None:
        host = _sac_env("HOST") or socket.gethostname().split(".")[0]

    imported = 0
    skipped = 0
    if not registry_dir.exists():
        return {"imported": 0, "skipped": 0}

    swept_at = now_iso()
    seen = {
        (r.get("name"), r.get("host"), r.get("started_at")) for r in all_instances()
    }
    for path in sorted(registry_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (
            json.JSONDecodeError,
            OSError,
        ):  # stx-allow: fallback (reason: malformed shard tolerated)
            skipped += 1
            continue
        name = data.get("name")
        started_at = data.get("started_at")
        if not (name and started_at):
            skipped += 1
            continue
        if (name, host, started_at) in seen:
            skipped += 1
            continue
        put_instance_record(
            {
                "id": new_uuid7(),
                "name": name,
                "host": host,
                "scope": "global",
                "pid": data.get("pid"),
                "screen": data.get("screen"),
                "workdir": data.get("workdir"),
                "started_at": started_at,
                "ended_at": swept_at,
                "exit_reason": "reboot-swept",
            }
        )
        seen.add((name, host, started_at))
        imported += 1
    return {"imported": imported, "skipped": skipped}


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "_table_filter_clauses",
    "export_state",
    "import_state",
    "import_legacy_registry",
]
