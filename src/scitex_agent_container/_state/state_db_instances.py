"""``instances`` lifecycle CRUD for state.db.

Extracted from :mod:`state_db` (which would otherwise exceed the
512-line per-file cap after the sac-agent-spawn family-tree columns
landed). DDL for the ``instances`` table still lives in
:mod:`state_db`; this module owns the row WRITE/READ helpers and is
re-exported from :mod:`state_db` so ``from ...state_db import
record_instance_start`` keeps working for every existing caller.

Family-tree columns (sac-agent-spawn design, Rule B/D):

  * ``bound_port`` — the ACTUAL bound a2a port. Written together with
    the legacy ``a2a_port`` so new readers can prefer ``bound_port``
    while legacy ``a2a_port`` callers keep working unchanged.
  * ``remote`` — ``1`` when the row was written for an agent that
    landed on a DIFFERENT host (cross-host dispatch); ``0`` for a
    local start.
  * ``spawned_by`` — the launching identity (parent agent name, or
    ``"cli"`` when launched from the bare CLI / lead). The lineage
    edge the family-tree DAG is reconstructed from.

Following the sibling-module convention (``state_db_diary``,
``state_db_heartbeats``), :func:`state_db.open_db` and the id/clock
helpers are imported lazily inside each function so :mod:`state_db`
can re-export from here without a circular import at module load.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state_db_hostname import resolve_host as _resolve_host


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
    bound_port: int | None = None,
    remote: bool = False,
    spawned_by: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Insert an ``instances`` row for a freshly-started agent.

    Returns the new ``instance_id`` (uuid7). Also appends a
    ``kind='start'`` row to ``events``.

    The family-tree columns make every start — local OR cross-host
    dispatch — record its bound port, host, lineage and locality as an
    intrinsic side-effect (sac-agent-spawn design, Rule B). When
    ``bound_port`` is not given it defaults to ``a2a_port`` so a caller
    that only knows the resolved port still populates both columns.
    """
    from .state_db import new_uuid7, now_iso, open_db

    instance_id = new_uuid7()
    started_at = now_iso()
    canonical_host = _resolve_host(host)
    if bound_port is None:
        bound_port = a2a_port
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO instances (
                id, definition_id, name, host, scope,
                pid, ppid, screen, workdir, a2a_port, started_at,
                bound_port, remote, spawned_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                bound_port,
                1 if remote else 0,
                spawned_by,
            ),
        )
        conn.execute(
            "INSERT INTO events (ts, instance_id, kind, actor) "
            "VALUES (?, ?, 'start', 'sac')",
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

    Idempotent: stopping an already-stopped row is a no-op.
    """
    from .state_db import now_iso, open_db

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


def list_active_instances(
    host: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return every ``ended_at IS NULL`` row, optionally host-filtered."""
    from .state_db import open_db

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


def last_known_instance(
    name: str,
    db_path: Path | None = None,
) -> dict | None:
    """Return the most-recent ``instances`` row for ``name``, active OR ended.

    Unlike :func:`list_active_instances` (which filters ``ended_at IS
    NULL``), this returns the latest row regardless of lifecycle state so a
    fail-loud resolver can report the LAST KNOWN host + ``started_at`` +
    whether the row has ``ended_at`` set. ``None`` only when the agent name
    has never appeared in this host's cross-host registry.

    This is the evidence behind the #192 fail-loud message: when an agent
    cannot be resolved to a live instance, the resolver must name the last
    known placement rather than silently assume the agent is local.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM instances WHERE name=? ORDER BY started_at DESC LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None


__all__ = [
    "last_known_instance",
    "list_active_instances",
    "record_instance_start",
    "record_instance_stop",
]
