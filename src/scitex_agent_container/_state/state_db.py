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
from .state_db_hostname import resolve_host as _resolve_host  # noqa: F401
from .state_db_migrations import (
    migrate_instance_heartbeats_add_seq,
    migrate_instances_add_family_tree_cols,
    migrate_legacy_heartbeats,
)

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        os.path.expanduser("~/.scitex/agent-container/runtime/state.db"),
    )
)


# Registry tables (F-CS11) — definitions, instances, events.
# The legacy ``heartbeats`` table (instance_id, ts, ...) is now
# created under the name ``instance_heartbeats``. See _SCHEMA_DIARY
# below for the new diary-style ``heartbeats``.
_SCHEMA_REGISTRY = """
CREATE TABLE IF NOT EXISTS definitions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    yaml_path       TEXT NOT NULL,
    yaml_sha256     TEXT NOT NULL,
    scope           TEXT NOT NULL,
    runtime         TEXT,
    first_seen_at   TEXT NOT NULL,
    UNIQUE(yaml_path, yaml_sha256)
);

CREATE TABLE IF NOT EXISTS instances (
    id                  TEXT PRIMARY KEY,
    definition_id       TEXT REFERENCES definitions(id),
    name                TEXT NOT NULL,
    host                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    pid                 INTEGER,
    ppid                INTEGER,
    screen              TEXT,
    workdir             TEXT,
    a2a_port            INTEGER,
    started_at          TEXT NOT NULL,
    last_heartbeat_at   TEXT,
    ended_at            TEXT,
    exit_reason         TEXT,
    iter_count          INTEGER DEFAULT 0,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    -- Family-tree / cross-host columns (sac-agent-spawn design, Rule
    -- B/D). ``bound_port`` mirrors ``a2a_port`` for new readers (both
    -- written together so legacy ``a2a_port`` callers keep working);
    -- ``remote`` is 1 for a cross-host-dispatched agent; ``spawned_by``
    -- is the launching identity ("cli"/parent-agent-name) — the lineage
    -- edge the spawn DAG is reconstructed from.
    bound_port          INTEGER,
    remote              INTEGER DEFAULT 0,
    spawned_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_instances_active
    ON instances(name, host, scope) WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_instances_host
    ON instances(host);

-- ``seq`` (AUTOINCREMENT) gives a total insertion order so "latest
-- heartbeat" is MAX(seq) — deterministic regardless of ``ts``
-- (second-resolution) ties. ``UNIQUE(instance_id, ts)`` keeps the
-- same-second collapse via the ON CONFLICT upsert in update_heartbeat.
-- See state_db_heartbeats / state_db_migrations for the full rationale.
CREATE TABLE IF NOT EXISTS instance_heartbeats (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL REFERENCES instances(id),
    ts              TEXT NOT NULL,
    iter            INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    pane_state      TEXT,
    UNIQUE (instance_id, ts)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    instance_id     TEXT,
    definition_id   TEXT,
    kind            TEXT NOT NULL,
    actor           TEXT,
    payload_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_instance
    ON events(instance_id, ts);
"""

# Attempts predates state.db (lived in actions.db). Bundled here so
# state.db is self-contained on a fresh host.
_SCHEMA_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    agent        TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    elapsed_s    REAL    NOT NULL,
    pane_before  TEXT,
    pane_after   TEXT,
    extras       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
CREATE INDEX IF NOT EXISTS idx_attempts_agent_action ON attempts(agent, action);
"""

# Diary tables (2026-05-17): turns / errors / heartbeats. Each
# agent appends rows like a journal; the lead reads + filters.
_SCHEMA_DIARY = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id        TEXT NOT NULL,
    name           TEXT NOT NULL,
    host           TEXT NOT NULL,
    status         TEXT NOT NULL,
    prompt_text    TEXT,
    response_text  TEXT,
    ts             REAL NOT NULL,
    session_id     TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_turns_turn_id ON turns(turn_id);
CREATE INDEX IF NOT EXISTS idx_turns_name_ts ON turns(name, ts);

CREATE TABLE IF NOT EXISTS errors (
    error_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    host       TEXT NOT NULL,
    cause      TEXT NOT NULL,
    detail     TEXT,
    ts         REAL NOT NULL,
    turn_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_name_ts ON errors(name, ts);
CREATE INDEX IF NOT EXISTS idx_errors_cause ON errors(cause);

CREATE TABLE IF NOT EXISTS heartbeats (
    heartbeat_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    pid           INTEGER,
    state         TEXT NOT NULL,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_name_ts ON heartbeats(name, ts);

-- WI-1 channel-event durability (handoff §4 "Durability /
-- replay-on-reconnect"): persist every channel-bus event so a POST
-- with no subscriber is delivered on connect, and a kill+reconnect
-- replays exactly the missed events.
--
-- ``id`` is the SSE-cursor (the value of the SSE ``id:`` line); a
-- reconnecting client passes it back as ``Last-Event-ID`` to resume
-- without dropping or duplicating events.
-- ``meta_json`` carries the full minted envelope so the inbox bus can
-- replay byte-identical frames after a process restart.
-- ``delivered_at`` is set the first time the event reaches a live
-- subscriber; NULL means "still waiting on the bus".
CREATE TABLE IF NOT EXISTS channel_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target        TEXT NOT NULL,
    source        TEXT,
    kind          TEXT NOT NULL DEFAULT 'message',
    content       TEXT,
    meta_json     TEXT NOT NULL,
    ts            REAL NOT NULL,
    delivered_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_channel_events_target_undelivered
    ON channel_events(target, id) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_channel_events_target_id
    ON channel_events(target, id);

-- WI-2 ACL — authenticated identity, lineage edges, cross-group grants
-- (handoff §4; lead 2026-05-21 RESTORED the authenticated-identity
-- criterion the prior limited scope had deferred).
--
-- ``node_tokens`` is the authenticated-identity primitive. Each node
-- (sac-managed or external) gets a token minted at registration; the
-- listen server resolves an incoming ``Authorization: Bearer <token>``
-- to a node name via :class:`_listen._acl.NodeAuthMiddleware`. The
-- acceptance "identity cannot be spoofed via a metadata field"
-- (handoff §4) is enforced by ``check_send_acl``: when a per-node
-- bearer is presented, ``metadata.from_agent`` MUST match the bearer's
-- resolved name — a mismatch is a 403 with an explicit spoof reason.
--
-- ``lineage`` records parent → child edges produced by
-- ``sac agents start``. A node's *group* (the default-ACL unit) is
-- derived from lineage: parent + parent's direct children. Schema
-- stays N-level capable — see derive_group() for the traversal.
--
-- ``comms_grants`` records explicit cross-group send grants. A row
-- ``(sender, target)`` permits ``sender → target`` even when the
-- two are in different groups. With authenticated identity in force,
-- ``sender`` is the resolved-from-bearer name (administrative caller
-- path: the host-wide bearer honours ``metadata.from_agent`` verbatim
-- — used by cross-host forwarders authenticating with the
-- destination's host bearer pulled from ``peer-tokens/`` registry).
CREATE TABLE IF NOT EXISTS node_tokens (
    name        TEXT PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_tokens_token ON node_tokens(token);

CREATE TABLE IF NOT EXISTS lineage (
    child_name   TEXT PRIMARY KEY,
    parent_name  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_parent ON lineage(parent_name);

CREATE TABLE IF NOT EXISTS comms_grants (
    sender_name  TEXT NOT NULL,
    target_name  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    note         TEXT,  -- optional audit annotation
    PRIMARY KEY (sender_name, target_name)
);
CREATE INDEX IF NOT EXISTS idx_comms_grants_target ON comms_grants(target_name);
"""

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
        conn.executescript(_SCHEMA_ATTEMPTS)
        conn.executescript(_SCHEMA_DIARY)
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
