"""SQLite-backed state for scitex-agent-container (F-CS11).

Replaces the per-agent JSON files under
``~/.scitex/agent-container/registry/`` with a single ``state.db``
holding four tables:

  * ``definitions`` — yaml on disk (one row per ``(yaml_path, sha256)``
    pair). Surfaces a stable id even when the yaml is renamed.
  * ``instances`` — each call to ``sac agent start`` writes a row.
    ``instances.id`` is a uuid7 generated at start time and is THE
    identity (not pid). ``host`` is the canonical alias-resolved
    hostname so cross-host queries don't collide.
  * ``heartbeats`` — append-only time series. Prunable.
  * ``events`` — audit log spanning definitions + instances.

The single-file layout makes backup/sync trivial (one ``cp``) and
keeps the existing ``actions.db`` table (``attempts``) co-located so
queries can join across action history and instance lifecycle.

Background: F-CS12 (multi-host) reads ``instances.host``;
F-CS14 (orochi consumption) walks ``instances`` and ``heartbeats``
via ``sac db export --since <ts>``. Schema is forward-compatible
with both.

This module is **additive**. Existing reads/writes against
``registry/*.json`` continue to work; ``sac db migrate`` lifts the
JSON shards into ``instances`` rows in one shot. Phase 2 will switch
hot paths over and retire the directory.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        os.path.expanduser("~/.scitex/agent-container/state.db"),
    )
)


# Schema is split into two groups: registry tables (added by F-CS11)
# and the legacy ``attempts`` table (already present in actions.db
# under sac < F-CS11). The migration helper attaches the existing
# actions.db file or copies its rows so everything lives in state.db.
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
    output_tokens       INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_instances_active
    ON instances(name, host, scope) WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_instances_host
    ON instances(host);

CREATE TABLE IF NOT EXISTS heartbeats (
    instance_id     TEXT NOT NULL REFERENCES instances(id),
    ts              TEXT NOT NULL,
    iter            INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    pane_state      TEXT,
    PRIMARY KEY (instance_id, ts)
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

# The ``attempts`` table predates state.db (it lived in actions.db).
# Including the schema makes state.db self-contained on a fresh host
# and lets ``sac db migrate`` pull the rows over without an ATTACH.
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

# Tables exposed by `sac db query --table=<t>`. Whitelisted so users
# can't pass arbitrary identifiers through Python's str-format SQL
# (sqlite parameter substitution doesn't support table names).
KNOWN_TABLES = ("definitions", "instances", "heartbeats", "events", "attempts")


def now_iso() -> str:
    """ISO-8601 UTC with trailing 'Z' (matches the legacy registry format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid7() -> str:
    """Return a uuid7 string (time-ordered, sortable by start time).

    Falls back to uuid4 if uuid.uuid7 is unavailable on the runtime
    Python (added in 3.14). uuid4 is acceptable: collision risk is
    negligible at our scale and ``started_at`` already gives time
    ordering for queries.
    """
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    return str(uuid.uuid4())


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL: better write concurrency, smaller commit fsync cost. Safe
    # for the single-host workload sac runs today; the file stays
    # SQLite-compatible for ssh-cp.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path: Path | None = None) -> Path:
    """Create state.db with all tables if missing. Idempotent.

    Returns the resolved database path.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    with _connect(path) as conn:
        conn.executescript(_SCHEMA_REGISTRY)
        conn.executescript(_SCHEMA_ATTEMPTS)
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
    """Return ``{table_name: row_count}`` for every known table.

    Used by ``sac db show`` to give the operator a quick health check.
    """
    counts: dict[str, int] = {}
    with open_db(db_path) as conn:
        for table in KNOWN_TABLES:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
    return counts


def import_legacy_registry(
    registry_dir: Path,
    db_path: Path | None = None,
    host: str | None = None,
) -> dict[str, int]:
    """Lift the JSON files under ``registry_dir`` into ``instances``.

    Each JSON shard becomes one ``instances`` row marked
    ``exit_reason='reboot-swept'`` with ``ended_at`` = now (post-import
    they're definitionally not running). Idempotent: existing rows
    matched by ``(name, host, started_at)`` are skipped.

    Args:
        registry_dir: Path to the legacy ``registry/`` directory.
        db_path: Override the state.db location (mostly for tests).
        host: Canonical hostname for the imported rows. Defaults to
            ``$SAC_HOST`` else ``hostname -s``. Stored verbatim;
            full alias resolution lands in F-CS12.

    Returns:
        ``{"imported": N, "skipped": M}``.
    """
    import json
    import socket

    if host is None:
        host = os.environ.get("SAC_HOST") or socket.gethostname().split(".")[0]

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
            # Skip if this exact (name, host, started_at) is already there.
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
