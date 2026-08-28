"""Instance-heartbeat write + read helpers for state.db.

Extracted from :mod:`state_db` so that module stays under the per-file
line cap. The DDL for ``instance_heartbeats`` lives in :mod:`state_db`
(``_SCHEMA_REGISTRY``); this module owns the WRITE path
(:func:`update_heartbeat`) and the canonical "latest row" READ
(:func:`latest_instance_heartbeat`).

Determinism note (the reason this code is careful about ``seq``):
``instance_heartbeats`` is keyed by a monotonic
``seq INTEGER PRIMARY KEY AUTOINCREMENT``, NOT by the second-resolution
``ts``. Two heartbeats in the same wall-clock second collapse into one
row via the ``ON CONFLICT(instance_id, ts)`` upsert; two beats that
straddle a second boundary produce two rows. Either way, "the latest
heartbeat" is unambiguously ``MAX(seq)`` — never an arbitrary tie on
``ts``. Selecting the latest row by ``ts`` (or with no ORDER BY) is the
non-determinism this design eliminates.

``open_db`` is imported lazily inside each function to avoid a circular
import: :mod:`state_db` re-exports :func:`update_heartbeat` from here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable


def update_heartbeat(
    instance_id: str,
    *,
    iter: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    pane_state: str | None = None,
    db_path: Path | None = None,
    now_fn: Callable[[], str] | None = None,
) -> None:
    """Append an instance-heartbeat row + bump the rolling cache.

    The duplicated state on ``instances`` lets ``sac agent status``
    answer 'is this agent still doing work?' without a JOIN. Same-second
    beats merge into one ``instance_heartbeats`` row (last-non-NULL
    wins per column); the ``seq`` PK keeps "latest" deterministic when
    beats straddle a second.

    ``now_fn`` is an injection seam (defaults to :func:`state_db.now_iso`)
    so callers — including tests — can pin the heartbeat ``ts`` to a real
    deterministic value instead of the wall clock. Passing a fixed value
    forces same-second collapse; passing distinct values forces the
    straddle-a-second path.
    """
    from .state_db import now_iso, open_db
    from .state_db_instances import touch_instance_counters

    ts = (now_fn or now_iso)()
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO instance_heartbeats (
                instance_id, ts, iter, input_tokens, output_tokens, pane_state
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id, ts) DO UPDATE SET
                iter          = COALESCE(excluded.iter, instance_heartbeats.iter),
                input_tokens  = COALESCE(excluded.input_tokens, instance_heartbeats.input_tokens),
                output_tokens = COALESCE(excluded.output_tokens, instance_heartbeats.output_tokens),
                pane_state    = COALESCE(excluded.pane_state, instance_heartbeats.pane_state)
            """,
            (instance_id, ts, iter, input_tokens, output_tokens, pane_state),
        )
    # THE TWO HALVES NOW LIVE IN DIFFERENT ENGINES, 2026-08-28. The time
    # series above is still SQLite (``instance_heartbeats`` has not moved);
    # the rolling cache below is the ``instances`` record, which has. This
    # is the transitional shape and it is worth naming rather than hiding:
    # the write is no longer one transaction, so a crash between the two
    # can leave a beat recorded with the cache un-bumped. That direction is
    # the safe one — the cache is a denormalisation whose only consumer is
    # "is this agent still working?", and a stale-looking cache is a false
    # DEAD, which the GC's own staleness threshold already tolerates. The
    # reverse order would have produced a false ALIVE.
    touch_instance_counters(
        instance_id,
        last_heartbeat_at=ts,
        iter=iter,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def latest_instance_heartbeat(
    instance_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: Path | None = None,
) -> dict | None:
    """Return the latest ``instance_heartbeats`` row, or ``None``.

    "Latest" is ``MAX(seq)`` — the monotonic insertion order — so the
    answer is deterministic regardless of how close in wall-clock time
    the beats arrived. Pass an open ``conn`` to read within an existing
    transaction; otherwise a short-lived connection is opened.
    """

    def _query(c: sqlite3.Connection) -> dict | None:
        row = c.execute(
            "SELECT * FROM instance_heartbeats "
            "WHERE instance_id=? ORDER BY seq DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    if conn is not None:
        return _query(conn)

    from .state_db import open_db

    with open_db(db_path) as c:
        return _query(c)
