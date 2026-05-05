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

import json
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


def _resolve_host(host: str | None) -> str:
    """Canonical hostname for state.db writes.

    Resolution chain (matches F-CS12 spec):
        1. ``host`` arg (explicit override)
        2. ``$SAC_HOST`` env var
        3. ``hostname -s`` (short form)

    Full alias resolution against ``sac.yaml`` lands in F-CS12; this
    helper just gives the registry a stable string to scope against.
    """
    if host:
        return host
    import socket

    return os.environ.get("SAC_HOST") or socket.gethostname().split(".")[0]


def record_instance_start(
    name: str,
    *,
    pid: int | None = None,
    ppid: int | None = None,
    screen: str | None = None,
    workdir: str | None = None,
    a2a_port: int | None = None,
    scope: str = "global",
    host: str | None = None,
    definition_id: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Insert an ``instances`` row for a freshly-started agent.

    Returns the new ``instance_id`` (uuid7). Caller is expected to
    persist this id alongside the runner's PID file so subsequent
    heartbeat / stop calls can target the right row.

    Also appends a ``kind='start'`` row to ``events`` so the audit
    log captures the lifecycle transition.
    """
    instance_id = new_uuid7()
    started_at = now_iso()
    canonical_host = _resolve_host(host)
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO instances (
                id, definition_id, name, host, scope,
                pid, ppid, screen, workdir, a2a_port, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                definition_id,
                name,
                canonical_host,
                scope,
                pid,
                ppid,
                screen,
                workdir,
                a2a_port,
                started_at,
            ),
        )
        conn.execute(
            "INSERT INTO events (ts, instance_id, kind, actor) VALUES (?, ?, 'start', 'sac')",
            (started_at, instance_id),
        )
    return instance_id


def record_instance_stop(
    instance_id: str,
    *,
    exit_reason: str = "stopped",
    db_path: Path | None = None,
) -> bool:
    """Mark an instance as ended. Returns True iff a row was updated.

    Idempotent: stopping an already-stopped row is a no-op (the
    update touches only rows where ``ended_at IS NULL``).
    """
    ended_at = now_iso()
    with open_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE instances SET ended_at=?, exit_reason=? "
            "WHERE id=? AND ended_at IS NULL",
            (ended_at, exit_reason, instance_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "INSERT INTO events (ts, instance_id, kind, actor, payload_json) "
            "VALUES (?, ?, 'stop', 'sac', ?)",
            (ended_at, instance_id, json.dumps({"exit_reason": exit_reason})),
        )
    return True


def update_heartbeat(
    instance_id: str,
    *,
    iter: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    pane_state: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Append a heartbeat row + bump the rolling fields on the instance.

    The duplicated state on ``instances`` (``last_heartbeat_at``,
    ``iter_count``, ``input_tokens``, ``output_tokens``) lets ``sac
    agent status`` answer 'is this agent still doing work?' without
    a JOIN — the heartbeats table is the authoritative time series,
    the columns on ``instances`` are a cache for the hot read.
    """
    ts = now_iso()
    with open_db(db_path) as conn:
        # Tolerate same-second collisions (the (instance_id, ts) PK
        # rejects rapid duplicates at 1-second resolution; merge the
        # latest-known fields onto the existing row instead of failing).
        conn.execute(
            """
            INSERT INTO heartbeats (
                instance_id, ts, iter, input_tokens, output_tokens, pane_state
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id, ts) DO UPDATE SET
                iter          = COALESCE(excluded.iter, heartbeats.iter),
                input_tokens  = COALESCE(excluded.input_tokens, heartbeats.input_tokens),
                output_tokens = COALESCE(excluded.output_tokens, heartbeats.output_tokens),
                pane_state    = COALESCE(excluded.pane_state, heartbeats.pane_state)
            """,
            (instance_id, ts, iter, input_tokens, output_tokens, pane_state),
        )
        # Bump rolling cache. COALESCE keeps the previous value when
        # the caller didn't pass that field this turn.
        conn.execute(
            """
            UPDATE instances
               SET last_heartbeat_at = ?,
                   iter_count    = COALESCE(?, iter_count),
                   input_tokens  = COALESCE(?, input_tokens),
                   output_tokens = COALESCE(?, output_tokens)
             WHERE id = ?
            """,
            (ts, iter, input_tokens, output_tokens, instance_id),
        )


def list_active_instances(
    host: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return every ``ended_at IS NULL`` row, optionally host-filtered."""
    with open_db(db_path) as conn:
        if host is None:
            cur = conn.execute(
                "SELECT * FROM instances WHERE ended_at IS NULL "
                "ORDER BY started_at DESC"
            )
        else:
            cur = conn.execute(
                "SELECT * FROM instances WHERE ended_at IS NULL AND host=? "
                "ORDER BY started_at DESC",
                (host,),
            )
        return [dict(r) for r in cur.fetchall()]


def _proc_btime() -> str | None:
    """Return Linux boot time as ISO-8601 UTC, or None on non-Linux.

    Used by ``gc_dead_instances`` to mark every instance whose
    ``started_at`` predates the current boot as ``reboot-swept``.
    No /proc/stat → no boot detection (we silently skip the sweep).
    """
    # stx-allow: fallback (reason: /proc/stat is Linux-specific; macOS
    # has no equivalent and the reboot-sweep degrades gracefully)
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    return datetime.fromtimestamp(btime, timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        pass
    return None


def gc_dead_instances(
    *,
    db_path: Path | None = None,
    heartbeat_stale_seconds: int = 300,
) -> dict[str, int]:
    """Sweep instances whose runner is gone. Returns counters.

    Three heuristics, applied in order:

    1. **Boot-epoch check** — every active row whose ``started_at``
       precedes the current ``/proc/stat btime`` is marked
       ``exit_reason='reboot-swept'``.
    2. **PID liveness** — for the host's own active rows, ``kill -0
       pid`` failures mark the row ``exit_reason='crashed'``.
    3. **Heartbeat staleness** — if ``last_heartbeat_at`` exists and
       is older than ``heartbeat_stale_seconds``, mark
       ``exit_reason='gc-stale'``.

    Cross-host instances are NOT swept (we have no liveness signal
    for them; F-CS12 will add ssh-based probing).
    """
    import socket

    counters = {"reboot_swept": 0, "crashed": 0, "gc_stale": 0}
    boot = _proc_btime()
    canonical_host = _resolve_host(None)
    now_ts = now_iso()
    stale_cutoff = datetime.now(timezone.utc).timestamp() - heartbeat_stale_seconds

    with open_db(db_path) as conn:
        # 1. boot-epoch — applies to all hosts; if a row's started_at
        # precedes the current boot, the runner can't possibly be alive.
        if boot is not None:
            cur = conn.execute(
                "UPDATE instances SET ended_at=?, exit_reason='reboot-swept' "
                "WHERE ended_at IS NULL AND host=? AND started_at < ?",
                (boot, canonical_host, boot),
            )
            counters["reboot_swept"] = cur.rowcount

        # 2. pid liveness — local rows only; remote requires ssh (F-CS12).
        rows = conn.execute(
            "SELECT id, pid FROM instances WHERE ended_at IS NULL AND host=?",
            (canonical_host,),
        ).fetchall()
        for row in rows:
            pid = row["pid"]
            if pid is None or pid <= 0:
                continue
            # stx-allow: fallback (reason: kill -0 errors when pid is dead OR
            # not ours; both cases mean 'not alive from our POV')
            try:
                os.kill(pid, 0)
            except (
                OSError,
                ProcessLookupError,
            ):  # stx-allow: fallback (reason: see inline comment)
                conn.execute(
                    "UPDATE instances SET ended_at=?, exit_reason='crashed' WHERE id=?",
                    (now_ts, row["id"]),
                )
                counters["crashed"] += 1

        # 3. heartbeat staleness — anything with a heartbeat_at older
        # than the cutoff is presumed wedged.
        cur = conn.execute(
            "SELECT id, last_heartbeat_at FROM instances "
            "WHERE ended_at IS NULL AND last_heartbeat_at IS NOT NULL"
        ).fetchall()
        for row in cur:
            try:
                hb = (
                    datetime.strptime(row["last_heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except (
                ValueError,
                TypeError,
            ):  # stx-allow: fallback (reason: malformed timestamp tolerated)
                continue
            if hb < stale_cutoff:
                conn.execute(
                    "UPDATE instances SET ended_at=?, exit_reason='gc-stale' "
                    "WHERE id=?",
                    (now_ts, row["id"]),
                )
                counters["gc_stale"] += 1

    # Suppress shadowing the canonical hostname helper.
    _ = socket
    return counters


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
