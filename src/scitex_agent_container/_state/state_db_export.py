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
      "instances":      [ {row}, ... ],
      "channel_events": [ {row}, ... ],
      ...
    }
  }

Filtering: each table picks a sensible "advance" column and emits
only rows where that column >= since (or all rows when since is None).
``instances`` emits when *either* its start timestamp OR its end
timestamp is >= since — an aggregator needs both halves of the
lifecycle. (``definitions`` shared that two-sided rule until 2026-08-28,
when it left :data:`KNOWN_TABLES` alongside ``instance_heartbeats`` and
``events``; see the map below.)
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
        "instances": (
            "WHERE started_at >= ? OR ended_at >= ?",
            (since, since),
        ),
        # ``definitions`` had a ``WHERE first_seen_at >= ?`` entry here, and
        # ``instance_heartbeats`` and ``events`` a ``WHERE ts >= ?`` each,
        # until 2026-08-28. All three left KNOWN_TABLES that day, so none of
        # the three mappings could ever be selected again — and a WHERE
        # clause naming a table SQLite no longer has reads as "sac still
        # exports this", which for ``events`` in particular would be the
        # wrong promise twice over: the rows still sit on old databases, and
        # this filter is exactly what would have kept shipping them to a
        # peer as though something on the far side read them.
        # ``attempts`` had a ``WHERE ts >= ?`` entry here until 2026-08-28.
        # It left KNOWN_TABLES that day -- zero writers, DDL deleted -- so
        # this mapping could never be selected again, and a WHERE clause
        # naming a table SQLite no longer has reads as "sac still exports
        # this".
        # ``turns``, ``errors`` and the diary-style ``heartbeats`` each had a
        # ``WHERE ts >= ?`` entry here until 2026-08-28. All three moved to
        # per-host PostgreSQL and left KNOWN_TABLES together, so — exactly as
        # for acl_deny_notify_log below — these mappings could never be
        # selected again, and a WHERE clause naming a table SQLite no longer
        # has reads as "sac still exports this". This note used to add "note
        # ``instance_heartbeats`` above is a DIFFERENT table and has not
        # moved"; it is a different table and it has now gone too, though
        # not to PostgreSQL — it was deleted for having neither a caller nor
        # a row. See the note above.
        # WI-2 ACL tables — ``created_at`` is the row-mint time.
        # ``node_tokens`` had a ``WHERE created_at >= ?`` entry here until
        # 2026-08-28. The per-node bearer feature was removed that day --
        # zero callers, 0 rows on every host, DDL deleted -- so it left
        # KNOWN_TABLES and this mapping could never be selected again.
        # Deleting it matters more here than for the neighbours below: the
        # row this filter selected carried a bearer SECRET in its ``token``
        # column, and ``export_state`` ships every column of the tables it
        # is given. A filter naming it would read as "sac still exports
        # this", which for this one table would have been a description of
        # a credential leak rather than of a stale sync.
        "lineage": ("WHERE created_at >= ?", (since,)),
        # ``comms_nodes`` had a ``WHERE updated_at >= ?`` entry here until
        # 2026-08-28 — the ADR-0014 anti-entropy filter, written so a
        # tombstoned row still shipped on the next pull until both sides
        # converged. The table moved to the shared PostgreSQL store and left
        # KNOWN_TABLES, so this mapping could never be selected again. It is
        # deleted rather than kept for the reason the neighbours are, plus
        # one of its own: this table is the only one this module ever
        # EXISTED to sync, and a filter naming it would read as "sac still
        # ships the directory between hosts". It does not, and it must not —
        # every host now reads and writes the same directory, so an export /
        # import round trip could only re-insert a stale copy of rows the
        # peer already holds. THE STORE IS THE SYNC.
        # node_comms_policy's entry lived here until 2026-08-28. The table
        # moved to PostgreSQL and left KNOWN_TABLES, so this mapping could
        # never be selected again — and a WHERE clause naming a table SQLite
        # no longer has reads as "sac still exports this".
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
    """Dump the registry tables into a JSON-able dict.

    ``attempts`` was named here alongside them until 2026-08-28, when it
    left :data:`KNOWN_TABLES`; this dump follows that tuple, so it no
    longer ships an empty ``attempts`` array. ``definitions``,
    ``instance_heartbeats`` and ``events`` left the same tuple the same
    day and are gone from the dump for the same mechanical reason.

    Used by ``sac db export``; an aggregator consumes the result via
    ``sac db import`` (or its own importer).

    ``tables`` (added 2026-05 for ADR-0014's anti-entropy sync, which was
    retired with the ``comms_nodes`` move on 2026-08-28; the filter itself
    stays useful for any subset of the tables that remain)
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

    Each JSON shard becomes one ``instances`` row marked
    ``exit_reason='reboot-swept'`` with ``ended_at`` = now. Idempotent:
    existing rows matched by ``(name, host, started_at)`` are skipped.

    Returns ``{"imported": N, "skipped": M}``.
    """
    from .state_db import new_uuid7, now_iso, open_db

    if host is None:
        host = _sac_env("HOST") or socket.gethostname().split(".")[0]

    imported = 0
    skipped = 0
    if not registry_dir.exists():
        return {"imported": 0, "skipped": 0}

    swept_at = now_iso()
    with open_db(db_path) as conn:
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
            existing = conn.execute(
                "SELECT id FROM instances WHERE name=? AND host=? AND started_at=?",
                (name, host, started_at),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO instances (
                    id, name, host, scope, pid, screen, workdir,
                    started_at, ended_at, exit_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_uuid7(),
                    name,
                    host,
                    "global",
                    data.get("pid"),
                    data.get("screen"),
                    data.get("workdir"),
                    started_at,
                    swept_at,
                    "reboot-swept",
                ),
            )
            imported += 1
    return {"imported": imported, "skipped": skipped}


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "_table_filter_clauses",
    "export_state",
    "import_state",
    "import_legacy_registry",
]
