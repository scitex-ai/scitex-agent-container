"""SQLite-backed state for scitex-agent-container (F-CS11).

Replaces the per-agent JSON files under
``~/.scitex/agent-container/runtime/registry/`` with a single ``state.db``
holding tables in two groups:

  * F-CS11 registry — ``definitions``, ``instances``, ``events``.
  * F-CS11 phase 2 — ``instance_heartbeats`` (the legacy
    ``heartbeats`` time series, tied to an ``instances.id``).

The single-file layout makes backup/sync trivial (one ``cp``) and
keeps the existing ``actions.db`` table (``attempts``) co-located so
queries can join across action history and instance lifecycle.

THE DIARY GROUP IS GONE FROM SQLite (2026-08-28)
================================================
``turns`` / ``errors`` / ``heartbeats`` were a third group here: each
agent appended rows like a journal and the lead read them back. The
WRITERS moved to per-host PostgreSQL first; this module was the residue
— the DDL that kept creating the three empty tables, and the
``KNOWN_TABLES`` entries that kept ``sac db show`` and ``sac db query``
reading them.

Empty is the dangerous shape. ``sac db show`` reporting ``turns 0``
while PostgreSQL holds the rows is not a missing feature, it is a WRONG
ANSWER that looks like a right one — the failure the ``incarnations``
removal named on 2026-08-19. So the names are removed rather than left
whitelisted, and asking for one now fails loudly instead of answering
zero. :mod:`state_db_diary` owns the trio end to end.

NOTE: The original F-CS11 ``heartbeats`` table is renamed to
``instance_heartbeats`` on first open (idempotent migration in
``init_schema``). That migration STAYS: it is what an old state.db
still needs, and nothing creates the bare name here any more.

Large helper groups live in sibling modules, all re-exported from THIS
module so ``from ...state_db import X`` imports keep working:

  * :mod:`state_db_export` — export_state / import_state / import_legacy_registry.
  * :mod:`state_db_gc` — gc_dead_instances / _proc_btime.
  * :mod:`state_db_diary` — record_turn / record_error / record_heartbeat /
    latest_heartbeats_per_name. On PostgreSQL, NOT in this database.
  * :mod:`state_db_heartbeats` — update_heartbeat / latest_instance_heartbeat.
    ``instance_heartbeats``, which is a different table from the diary's
    ``heartbeats`` and has NOT moved.
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
)
from .state_db_schema import (
    _SCHEMA_ATTEMPTS,
    _SCHEMA_CHANNEL_AND_ACL,
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
    "channel_events",
    "node_tokens",
    "lineage",
    "comms_nodes",
    # ``comms_grants`` left on 2026-08-28 under the same ruling as
    # ``incarnations`` below. Its CRUD had already moved to the shared
    # PostgreSQL store (:mod:`.state_db_grants`, which resolves through
    # ``host_store``); only the DDL and this whitelist entry were left,
    # and the entry is what kept the GENERIC readers -- ``table_counts``
    # behind ``sac db show``, ``export_state``/``import_state``, and the
    # ``click.Choice`` for ``sac db query`` -- pointed at a SQLite table
    # nothing writes. The 52 live rows were carried into PostgreSQL
    # before this landed.
    # ``incarnations`` was here until 2026-08-19. It now lives in per-host
    # PostgreSQL via :mod:`.state_db_incarnations`, so it is NOT queryable
    # through `sac db query`. Removed rather than left behind: a whitelisted
    # name with no table returns an EMPTY result, and an empty result reads
    # as "this agent has no incarnations" when the truth is "you are asking
    # the wrong database". An unknown-table error is the honest answer.
    #
    # ``turns``, ``errors`` and ``heartbeats`` left on 2026-08-28 under the
    # SAME ruling, and the three go together because they share
    # :mod:`.state_db_diary` and the loops below. Every reader of this tuple
    # is generic — :func:`table_counts`, ``export_state``, ``import_state``,
    # and the ``--table`` choice list — so one name left behind here would
    # have kept `sac db show` printing ``turns 0`` while the rows sat in
    # PostgreSQL, and a half-migrated trio is a split brain that raises
    # nothing: some readers see a row, others do not.
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
        # The two ``node_comms_policy`` ADD COLUMN migrations ran here
        # until 2026-08-28. The table moved to PostgreSQL, so both would
        # now be permanent no-ops against a table SQLite no longer has —
        # dead code claiming a live purpose. Removed with the DDL.
        conn.executescript(_SCHEMA_ATTEMPTS)
        conn.executescript(_SCHEMA_CHANNEL_AND_ACL)
        # ``turns`` / ``errors`` / ``heartbeats`` were created by the
        # constant above (then called ``_SCHEMA_DIARY``) until 2026-08-28.
        # All three moved to per-host PostgreSQL; each diary store creates
        # its own schema on first open (``state_db_diary._open``), so there
        # is nothing to run here for them.
        # Task #27's two ACL tables were both created here until 2026-08-20.
        # ``pending_prompts`` and ``comms_blocks`` have BOTH moved to per-host
        # PostgreSQL; each store creates its own schema on first open
        # (``state_db_pending_approval.open_pending_prompt_store`` /
        # ``state_db_blocks.open_blocks_store``), so there is nothing to run
        # here for either. ``comms_grants`` was the last of that pair left
        # in SQLite; it moved to the shared PostgreSQL store and its DDL
        # was deleted on 2026-08-28, so nothing of the pair remains here.
        # The ``incarnations`` birth-certificate table used to be created
        # here. It moved to per-host PostgreSQL on 2026-08-19; the promise
        # this comment block used to make — "lives in the EXISTING sqlite
        # factory ON PURPOSE so the separately-carded sqlite→Postgres
        # migration carries it along" — is now kept. Its schema is created
        # on first open by :func:`state_db_incarnations.open_incarnation_store`,
        # so there is nothing to run here.
        # sac-comms item D's rate-limit log (acl_deny_notify_log) was created
        # here until 2026-08-20. It moved to per-host PostgreSQL alongside the
        # two task-#27 tables above; its schema is created on first open by
        # ``state_db_acl_deny_notify.open_deny_notify_store``.
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
