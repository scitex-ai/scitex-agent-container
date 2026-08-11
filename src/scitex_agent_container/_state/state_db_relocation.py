"""Where an agent lives, who holds the write lease, and how far a relocation got.

Three tables, one file, because they are written in one sequence and read
together. Each is the durable half of a module that was deliberately left pure:

    ``agent_residency``      :mod:`.._lifecycle._residency` — the stays
    ``relocation_leases``    :mod:`.._lifecycle._relocate_lease` — the fence
    ``relocation_journal``   :mod:`.._lifecycle._relocate_phases` — the steps

WHY THE DECISION MODULES HAVE NO STORE OF THEIR OWN. Their entire value is that
the rules can be tested with real values and no database; giving each one a
connection would have made the rules reachable only through I/O. So the rules
live there, the rows live here, and this module holds no policy — it does not
decide whether a move is legal, only how the answer is written down.

RESIDENCY IS THE HOST, AND THIS IS THE WRITE THAT MOVES IT. Before 2026-08-11
``host`` was a field in a git-tracked spec file that existed in one copy per
machine; the operator settled it (「設定ファイル、人が書くものはファイル、状態は
db」) and :mod:`.._lifecycle._relocate_host_record` became the single reader.
That reader has been answering from ``instances.host`` — the host of whichever
process happens to be running — which is genuinely true and cannot answer "which
host held this agent in March", because a stopped agent has no instance row and
therefore no residency at all. THAT is the gap this table closes, and it is why
a relocation can flip residency for an agent that is stopped on both hosts, which
is precisely the state it is in between SOURCE_STOP and TARGET_STANDBY.

AT MOST ONE OPEN STAY, ENFORCED BY THE WRITE RATHER THAN BY CONVENTION.
:func:`record_residency` closes any open stay in the same transaction as it opens
the new one, so "living on two hosts at once" is not a row combination that can
exist. A unique index would have been the tidier statement and cannot express it
— the constraint is on ``to_ts IS NULL`` per agent, and expressing that as a
partial unique index would leave the closing write free to fail halfway.

IDEMPOTENT, BECAUSE A COORDINATOR RE-RUNS. Recording a move to the host that is
already the open stay is a no-op that returns the existing row rather than an
error and rather than a second stay. A relocation that crashed after the write
and before the journal must be able to re-run without littering the history with
the evidence of its own retries.

NOTHING IS EVER DELETED. A closed stay stays closed and stays present; a journal
row is updated in place at its phase and keeps its steps. The migration fact is
the point (item #9): after a relocation completes, the record that it happened is
the only thing that can answer an attribution question later.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

__all__ = [
    "current_residency",
    "init_relocation_schema",
    "load_journal",
    "load_lease",
    "read_residency_history",
    "record_residency",
    "save_journal",
    "save_lease",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_residency (
    agent      TEXT NOT NULL,
    host       TEXT NOT NULL,
    from_ts    REAL NOT NULL,
    to_ts      REAL,
    seeded     INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_residency_agent
    ON agent_residency (agent, from_ts);

CREATE TABLE IF NOT EXISTS relocation_leases (
    agent      TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    fence      INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS relocation_journal (
    agent      TEXT PRIMARY KEY,
    from_host  TEXT NOT NULL,
    to_host    TEXT NOT NULL,
    phase      TEXT NOT NULL,
    steps      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def init_relocation_schema(db_path: Path | None = None) -> Path:
    """Create the three tables if missing. Idempotent."""
    from .state_db import init_schema, open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)
    return init_schema(db_path)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------
# residency
# --------------------------------------------------------------------------


def record_residency(
    *,
    agent: str,
    host: str,
    now: float | None = None,
    seeded_from_spec: bool = False,
    note: str = "",
    db_path: Path | None = None,
) -> bool:
    """Open a stay for ``agent`` on ``host``, closing whatever was open.

    Returns ``True`` when a new stay was opened and ``False`` when the agent was
    already recorded on that host — the second is a successful no-op, not a
    failure, and the caller distinguishes them only to report accurately.

    ``seeded_from_spec`` travels into the row so a value that came from a legacy
    spec field is not later mistaken for something that was measured. Provenance
    that is dropped at the moment of writing cannot be recovered by reading.
    """
    if not agent or not agent.strip():
        raise ValueError("record_residency needs the agent name")
    if not host or not host.strip():
        raise ValueError(
            "record_residency needs the host — an empty destination would open a "
            "residency that answers no question"
        )
    agent, host = agent.strip(), host.strip()
    ts = float(now) if now is not None else time.time()

    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT rowid, host FROM agent_residency "
            "WHERE agent=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1",
            (agent,),
        ).fetchone()
        if row is not None and (row[1] or "") == host:
            return False
        if row is not None:
            conn.execute(
                "UPDATE agent_residency SET to_ts=? WHERE rowid=?", (ts, row[0])
            )
        conn.execute(
            "INSERT INTO agent_residency (agent, host, from_ts, to_ts, seeded, note) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (agent, host, ts, 1 if seeded_from_spec else 0, note or None),
        )
    return True


def read_residency_history(agent: str, *, db_path: Path | None = None):
    """The agent's stays, oldest first, as :class:`.._lifecycle._residency.Residency`.

    Returns ``()`` for an agent this table has never heard of — genuinely "the db
    knows nothing", which is what lets a legacy spec ``host:`` seed it once and
    is deliberately distinct from a recorded stay that has since closed.
    """
    from .._lifecycle._residency import Residency
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        rows = conn.execute(
            "SELECT host, from_ts, to_ts FROM agent_residency "
            "WHERE agent=? ORDER BY from_ts ASC, rowid ASC",
            (agent,),
        ).fetchall()
    return tuple(
        Residency(
            host=r[0], from_ts=float(r[1]), to_ts=None if r[2] is None else float(r[2])
        )
        for r in rows
    )


def current_residency(agent: str, *, db_path: Path | None = None) -> str | None:
    """The host of the open stay, or ``None``. ``None`` is not a hostname."""
    from .._lifecycle._residency import current_host

    return current_host(read_residency_history(agent, db_path=db_path))


# --------------------------------------------------------------------------
# lease
# --------------------------------------------------------------------------


def save_lease(lease, *, db_path: Path | None = None) -> None:
    """Persist the single current lease for an agent, replacing any earlier one.

    One row per agent by primary key, so a second holder cannot be inserted
    alongside the first. The fence is what actually fences — an old holder that
    comes back reads this row, sees a fence above its own, and knows it is out —
    so the row is REPLACED rather than appended: there is exactly one answer to
    "who holds it", and a history of holders would invite reading the wrong one.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO relocation_leases (agent, holder, fence, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET holder=excluded.holder, "
            "fence=excluded.fence, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            (
                lease.agent,
                lease.holder,
                int(lease.fence),
                float(lease.expires_at),
                time.time(),
            ),
        )


def load_lease(agent: str, *, db_path: Path | None = None):
    """The stored lease for ``agent``, or ``None`` if nobody has ever held it."""
    from .._lifecycle._relocate_lease import Lease
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT holder, fence, expires_at FROM relocation_leases WHERE agent=?",
            (agent,),
        ).fetchone()
    if row is None:
        return None
    return Lease(
        agent=agent, holder=row[0], fence=int(row[1]), expires_at=float(row[2])
    )


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def save_journal(relocation, *, db_path: Path | None = None) -> None:
    """Write the relocation's phase and steps, replacing the previous row.

    One row per agent: a relocation is a thing that is happening to an agent, and
    two in flight at once is a state nobody should be able to represent, let
    alone resume from. The steps are stored whole (JSON) rather than as rows,
    because they are only ever read back as a unit and a partially-inserted
    journal would be worse than none.
    """
    from .state_db import open_db

    steps = json.dumps(
        [{"phase": s.phase, "at": s.at, "detail": s.detail} for s in relocation.steps]
    )
    with open_db(db_path) as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO relocation_journal (agent, from_host, to_host, phase, steps, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET from_host=excluded.from_host, "
            "to_host=excluded.to_host, phase=excluded.phase, steps=excluded.steps, "
            "updated_at=excluded.updated_at",
            (
                relocation.agent,
                relocation.from_host,
                relocation.to_host,
                relocation.phase,
                steps,
                time.time(),
            ),
        )


def load_journal(agent: str, *, db_path: Path | None = None):
    """The stored relocation for ``agent``, or ``None``.

    A stored row whose JSON will not parse returns ``None`` rather than raising:
    the caller's next move is to open a fresh relocation, and a corrupt journal
    must not make the agent unrelocatable. Nothing is deleted — the bad row stays
    for whoever wants to look at it.
    """
    from .._lifecycle._relocate_phases import Relocation, Step
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT from_host, to_host, phase, steps FROM relocation_journal WHERE agent=?",
            (agent,),
        ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row[3])
        steps = tuple(
            Step(phase=s["phase"], at=float(s["at"]), detail=s.get("detail", ""))
            for s in raw
        )
        return Relocation(
            agent=agent,
            from_host=row[0],
            to_host=row[1],
            phase=row[2],
            steps=steps,
        )
    except Exception:  # stx-allow: fallback (reason: an unparseable journal must not make an agent unrelocatable; the row is kept, not deleted, and the caller opens a fresh relocation)
        return None
