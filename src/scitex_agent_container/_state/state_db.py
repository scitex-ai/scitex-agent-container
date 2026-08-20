"""SQLite-backed state for scitex-agent-container (F-CS11 + diary tables).

Replaces the per-agent JSON files under
``~/.scitex/agent-container/runtime/registry/`` with a single ``state.db``
holding tables in three groups:

  * F-CS11 registry — ``definitions``, ``instances``, ``events``.
  * F-CS11 phase 2 — ``instance_heartbeats`` (the legacy
    ``heartbeats`` time series, tied to an ``instances.id``).
  * Diary (2026-05-17) — ``turns``, ``errors``, ``heartbeats``. Each
    agent writes here continuously, like a journal; the lead reads
    + filters when it wants cross-host visibility. ``heartbeats``
    promotes the per-agent ``heartbeat.json`` file into a queryable
    table keyed by ``(name, host, pid, state, ts)``.

The single-file layout makes backup/sync trivial (one ``cp``) and
keeps the existing ``actions.db`` table (``attempts``) co-located so
queries can join across action history and instance lifecycle.

NOTE: The original F-CS11 ``heartbeats`` table is renamed to
``instance_heartbeats`` on first open (idempotent migration in
``init_schema``) so the diary-style ``heartbeats`` can own the name.

Large helper groups live in sibling modules, all re-exported from THIS
module so ``from ...state_db import X`` imports keep working:

  * :mod:`state_db_export` — export_state / import_state / import_legacy_registry.
  * :mod:`state_db_gc` — gc_dead_instances / _proc_btime.
  * :mod:`state_db_diary` — record_turn / record_error / record_heartbeat /
    latest_heartbeats_per_name.
  * :mod:`state_db_heartbeats` — update_heartbeat / latest_instance_heartbeat.
  * :mod:`state_db_migrations` — idempotent schema migrations.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Re-exported for the 7 modules that ``from ...state_db import
# _resolve_host`` (claude_session, _node_channel, state_db_export,
# state_db_gc, _send, send_cmds, _dispatch). The ``instances`` CRUD
# that used it directly moved to state_db_instances; keep the
# re-export here so those import sites keep resolving.
from .._runtime_paths import runtime_base_dir
from .state_db_hostname import resolve_host as _resolve_host  # noqa: F401
from .state_db_migrations import (
    migrate_instance_heartbeats_add_seq,
    migrate_instances_add_family_tree_cols,
    migrate_legacy_heartbeats,
    migrate_node_comms_policy_add_group_name,
    migrate_node_comms_policy_add_group_names,
)
from .state_db_schema import (
    _SCHEMA_ATTEMPTS,
    _SCHEMA_DIARY,
    _SCHEMA_REGISTRY,
)

# ``SCITEX_AGENT_CONTAINER_STATE_DB`` still wins (explicit per-file
# override); its FALLBACK now routes through ``runtime_base_dir`` so the
# single ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` knob relocates state.db
# too. Unset env => identical to the historical
# ``~/.scitex/agent-container/runtime/state.db``.
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        str(runtime_base_dir() / "state.db"),
    )
)


# Tables exposed by `sac db query --table=<t>`. Whitelisted so users
# can't pass arbitrary identifiers through str-format SQL.
KNOWN_TABLES = (
    "definitions",
    "instances",
    "instance_heartbeats",
    "events",
    "attempts",
    "turns",
    "errors",
    "heartbeats",
    "channel_events",
    "node_tokens",
    "lineage",
    "comms_grants",
    "comms_nodes",
    "node_comms_policy",
    "acl_deny_notify_log",
    # ``incarnations`` was here until 2026-08-19. It now lives in per-host
    # PostgreSQL via :mod:`.state_db_incarnations`, so it is NOT queryable
    # through `sac db query`. Removed rather than left behind: a whitelisted
    # name with no table returns an EMPTY result, and an empty result reads
    # as "this agent has no incarnations" when the truth is "you are asking
    # the wrong database". An unknown-table error is the honest answer.
)


def now_iso() -> str:
    """ISO-8601 UTC with trailing 'Z' (matches the legacy registry format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid7() -> str:
    """Return a uuid7 string (time-ordered, sortable by start time).

    Falls back to uuid4 if uuid.uuid7 is unavailable on the runtime
    Python (added in 3.14).
    """
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    return str(uuid.uuid4())


def _default_connector(db_path: Path) -> sqlite3.Connection:
    """Default sqlite3 connection factory (test seam)."""
    return sqlite3.connect(db_path, timeout=30.0)


def _connect(
    db_path: Path,
    connector=_default_connector,
) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connector(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    current_mode = conn.execute("PRAGMA journal_mode").fetchone()
    if current_mode and str(current_mode[0]).lower() != "wal":
        import time

        for attempt in range(50):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 49:
                    raise
                time.sleep(0.02 * (attempt + 1))
    # GPFS / network-FS reliability. WAL (above) alone still floods
    # "disk I/O error" on Spartan GPFS under the heartbeat write loop
    # (cohort-A 2026-06-24, a SINGLE run -- not the %16-concurrency
    # case). scitex-db runs SQLite on GPFS reliably (neurovista) with
    # the tunings below; mirror its recipe. The IOERR is on WRITES, so
    # the load-bearing ones are synchronous=NORMAL (WAL-safe, far fewer
    # GPFS fsyncs) and temp_store=MEMORY (no transient temp files on
    # GPFS); mmap_size + wal_autocheckpoint complete scitex-db's set.
    # Best-effort per-PRAGMA: a tuning that errors on an exotic FS must
    # not kill an otherwise-usable connection.
    for _pragma in (
        "PRAGMA synchronous = NORMAL",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA mmap_size = 30000000000",
        "PRAGMA wal_autocheckpoint = 1000",
    ):
        try:
            conn.execute(_pragma)
        except sqlite3.Error:  # stx-allow: fallback (reason: tuning PRAGMAs are advisory; a failed one must not block open_db)
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path: Path | None = None) -> Path:
    """Create state.db with all tables if missing. Idempotent.

    Returns the resolved database path.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    with _connect(path) as conn:
        migrate_legacy_heartbeats(conn)
        migrate_instance_heartbeats_add_seq(conn)
        conn.executescript(_SCHEMA_REGISTRY)
        # ``executescript`` above creates ``instances`` fresh on a new
        # DB (with the family-tree columns) but is a no-op on an
        # existing one; the migration ADD COLUMNs them onto a pre-cols DB.
        migrate_instances_add_family_tree_cols(conn)
        # Same idempotent ADD COLUMN for the group-based-ACL ``group_name``
        # column on a pre-existing ``node_comms_policy`` (operator
        # 2026-06-25). No-op on a fresh DB (DDL already has the column).
        migrate_node_comms_policy_add_group_name(conn)
        # Same idempotent ADD COLUMN for the MULTI-value ``group_names``
        # column the authority gates read (incident 2026-08-10 — an agent
        # whose spec lists several groups was reduced to its FIRST one).
        migrate_node_comms_policy_add_group_names(conn)
        conn.executescript(_SCHEMA_ATTEMPTS)
        conn.executescript(_SCHEMA_DIARY)
        # Task #27 — ACL block/unblock flow tables. Both CREATE TABLE
        # scripts are idempotent; running them inline here means a
        # fresh state.db carries the tables without a separate
        # migration step. The owning modules expose the schema
        # strings; we pull them through the same connection so
        # ``init_schema`` stays atomic.
        from . import state_db_acl_deny_notify as _adn
        from . import state_db_blocks as _blocks
        from . import state_db_pending_approval as _pp

        conn.executescript(_pp._SCHEMA)
        conn.executescript(_blocks._SCHEMA)
        # The ``incarnations`` birth-certificate table used to be created
        # here. It moved to per-host PostgreSQL on 2026-08-19; the promise
        # this comment block used to make — "lives in the EXISTING sqlite
        # factory ON PURPOSE so the separately-carded sqlite→Postgres
        # migration carries it along" — is now kept. Its schema is created
        # on first open by :func:`state_db_incarnations.open_incarnation_store`,
        # so there is nothing to run here.
        # sac-comms item D (lead a2a c42b3e3c): rate-limit log for
        # synthetic ACL-deny notifications published at the target
        # receiver. One row per (sender, target) pair carrying the
        # last-notify timestamp; the check + update lives in
        # :func:`state_db_acl_deny_notify.should_notify_acl_deny`.
        conn.executescript(_adn._SCHEMA)
        conn.commit()
    return path


@contextmanager
def open_db(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Initialises schema on first use."""
    path = init_schema(db_path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_counts(db_path: Path | None = None) -> dict[str, int]:
    """Return ``{table_name: row_count}`` for every known table."""
    counts: dict[str, int] = {}
    with open_db(db_path) as conn:
        for table in KNOWN_TABLES:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
    return counts


# ``instances`` lifecycle CRUD (record_instance_start / _stop /
# list_active_instances) moved to :mod:`state_db_instances` under the
# per-file line cap; re-exported below so callers keep importing them
# from :mod:`state_db`.

# Re-export the helpers that used to live in this file but moved
# into sibling modules under the per-file line cap. Existing callers
# keep importing them from :mod:`state_db`.
from .state_db_diary import (  # noqa: E402,F401
    latest_heartbeats_per_name,
    record_error,
    record_heartbeat,
    record_turn,
)
from .state_db_export import (  # noqa: E402,F401
    EXPORT_SCHEMA_VERSION,
    export_state,
    import_legacy_registry,
    import_state,
)
from .state_db_export import (  # noqa: E402
    _table_filter_clauses as _table_filter_clauses_impl,
)
from .state_db_gc import (  # noqa: E402,F401
    _proc_btime,
    gc_dead_instances,
)
from .state_db_heartbeats import (  # noqa: E402,F401
    latest_instance_heartbeat,
    update_heartbeat,
)
from .state_db_instances import (  # noqa: E402,F401
    last_known_instance,
    list_active_instances,
    record_instance_start,
    record_instance_stop,
)


def _table_filter_clauses(since: str | None) -> dict[str, tuple[str, tuple]]:
    """Per-table SQL fragments + params for ``--since`` filtering.

    Thin wrapper over ``state_db_export._table_filter_clauses`` so the
    original module-level signature stays compatible with callers that
    only pass ``since``.
    """
    return _table_filter_clauses_impl(since, KNOWN_TABLES)
