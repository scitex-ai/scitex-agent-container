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

ATTEMPTS ACCUMULATE; THEY DO NOT REPLACE EACH OTHER. The journal was keyed on
``agent`` alone, so re-running after an abort overwrote the previous attempt's
row and the evidence of the first try was gone — for the one operation in the
fleet that moves a human's conversation between machines, and precisely at the
moment (a retry after a failure) when the previous attempt is what a reader
wants. The key is now ``(agent, attempt)``, and an attempt is identified by the
relocation's OWN opening timestamp (``steps[0].at``): a RESUMED journal carries
the same opening moment and updates its row, while a relocation opened afresh
gets the next attempt number. There is no flag to pass and no way for a caller
to get it wrong, because the discriminator is a fact the record already carries.

NOTHING IS EVER DELETED. A closed stay stays closed and stays present; a journal
row is updated in place at its phase and keeps its steps; an older schema's table
is RENAMED aside rather than dropped when the shape changes. The migration fact
is the point (item #9): after a relocation completes, the record that it happened
is the only thing that can answer an attribution question later.
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
    "load_journal_attempts",
    "load_lease",
    "read_residency_history",
    "record_residency",
    "save_journal",
    "save_lease",
]

#: The table the journal used before attempts accumulated. RENAMED, never
#: dropped: its rows are the only record of the relocations run under the old
#: one-row-per-agent key, and this feature's whole discipline is that nothing is
#: deleted — least of all an audit trail, during the migration that supersedes it.
JOURNAL_V1_TABLE = "relocation_journal_v1_one_row_per_agent"

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
    token      TEXT NOT NULL,
    fence      INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS relocation_journal (
    agent      TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    from_host  TEXT NOT NULL,
    to_host    TEXT NOT NULL,
    phase      TEXT NOT NULL,
    steps      TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (agent, attempt)
);
"""


def init_relocation_schema(db_path: Path | None = None) -> Path:
    """Create the three tables if missing, migrating older shapes. Idempotent."""
    from .state_db import init_schema, open_db

    with open_db(db_path) as conn:
        _ensure(conn)
    return init_schema(db_path)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The column names of ``table``, or an empty set when it does not exist."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring older table shapes forward. Runs BEFORE the CREATE IF NOT EXISTS.

    Order matters: on a fresh database every ``PRAGMA table_info`` is empty, so
    each branch is skipped and the schema below creates the current shape
    directly. On an existing one the old shape is detected by a MISSING COLUMN —
    the fact itself, not a version number a migration could forget to bump.
    """
    lease_cols = _columns(conn, "relocation_leases")
    if lease_cols and "token" not in lease_cols:
        # A lease row with no token cannot be presented (``Lease`` refuses an
        # empty one, deliberately — an empty token would pass every token
        # check). The column is added rather than the rows rewritten: inventing
        # a token for a holder that never received one would forge exactly the
        # credential the fence exists to make unforgeable.
        conn.execute(
            "ALTER TABLE relocation_leases ADD COLUMN token TEXT NOT NULL DEFAULT ''"
        )

    journal_cols = _columns(conn, "relocation_journal")
    if journal_cols and "attempt" not in journal_cols:
        conn.execute(f"ALTER TABLE relocation_journal RENAME TO {JOURNAL_V1_TABLE}")
        conn.executescript(_SCHEMA)
        # Every pre-migration row becomes attempt 1. Its opening timestamp was
        # never stored under the old shape, so ``updated_at`` stands in and is
        # the honest best available — it is only ever compared against a LIVE
        # relocation's opening moment, which cannot coincide with it.
        conn.execute(
            "INSERT INTO relocation_journal "
            "(agent, attempt, from_host, to_host, phase, steps, started_at, updated_at) "
            "SELECT agent, 1, from_host, to_host, phase, steps, updated_at, updated_at "
            f"FROM {JOURNAL_V1_TABLE}"
        )


def _ensure(conn: sqlite3.Connection) -> None:
    _migrate(conn)
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
            "INSERT INTO relocation_leases (agent, holder, token, fence, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET holder=excluded.holder, "
            "token=excluded.token, fence=excluded.fence, "
            "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (
                lease.agent,
                lease.holder,
                lease.token,
                int(lease.fence),
                float(lease.expires_at),
                time.time(),
            ),
        )


def load_lease(agent: str, *, db_path: Path | None = None):
    """The stored lease for ``agent``, or ``None`` if nobody has ever held it.

    THE TOKEN IS STORED AND READ BACK, and it has to be: every verb in
    :mod:`.._lifecycle._relocate_lease` except :func:`claim` requires the caller
    to PRESENT the token, and :class:`Lease` refuses an empty one because an
    empty token would satisfy every token check. An earlier version of this
    function reconstructed the lease without one and could not build a ``Lease``
    at all — the persistence layer for a decision module that had been carefully
    kept pure was, in effect, write-only.

    A row from before the token column carries ``''``. It is returned as ``None``
    — not as a lease — because a holder that cannot present a token cannot prove
    it holds anything, and treating it as held would leave the agent permanently
    unrelocatable behind a credential nobody has.
    """
    from .._lifecycle._relocate_lease import Lease
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT holder, token, fence, expires_at FROM relocation_leases WHERE agent=?",
            (agent,),
        ).fetchone()
    if row is None or not (row[1] or "").strip():
        return None
    return Lease(
        agent=agent,
        holder=row[0],
        token=row[1],
        fence=int(row[2]),
        expires_at=float(row[3]),
    )


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def save_journal(relocation, *, db_path: Path | None = None) -> int:
    """Write this ATTEMPT's phase and steps. Returns the attempt number written.

    ONE ROW PER ATTEMPT, not per agent. Which attempt this is comes from the
    relocation's own opening moment (``steps[0].at``): a record RESUMED from the
    store carries the timestamp its first run stamped, so it updates the row it
    already owns; a record from :func:`.._lifecycle._relocate_phases.begin`
    carries a new one and opens the next attempt. The caller passes nothing and
    therefore cannot get it wrong, and a retry after an abort no longer erases
    the attempt whose failure prompted it.

    Two relocations of one agent still cannot be IN FLIGHT at once — the resume
    path in the CLI loads the latest attempt and refuses a different destination
    — but the finished ones stay readable, which is the entire point of a
    journal for an operation that moves a human's conversation between machines.

    The steps are stored whole (JSON) rather than as rows, because they are only
    ever read back as a unit and a partially-inserted journal would be worse
    than none.
    """
    from .state_db import open_db

    steps = json.dumps(
        [{"phase": s.phase, "at": s.at, "detail": s.detail} for s in relocation.steps]
    )
    started_at = float(relocation.started_at)
    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT attempt FROM relocation_journal WHERE agent=? AND started_at=?",
            (relocation.agent, started_at),
        ).fetchone()
        if row is not None:
            attempt = int(row[0])
        else:
            highest = conn.execute(
                "SELECT MAX(attempt) FROM relocation_journal WHERE agent=?",
                (relocation.agent,),
            ).fetchone()
            attempt = (
                1 if highest is None or highest[0] is None else int(highest[0]) + 1
            )
        conn.execute(
            "INSERT INTO relocation_journal "
            "(agent, attempt, from_host, to_host, phase, steps, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent, attempt) DO UPDATE SET from_host=excluded.from_host, "
            "to_host=excluded.to_host, phase=excluded.phase, steps=excluded.steps, "
            "updated_at=excluded.updated_at",
            (
                relocation.agent,
                attempt,
                relocation.from_host,
                relocation.to_host,
                relocation.phase,
                steps,
                started_at,
                time.time(),
            ),
        )
    return attempt


def load_journal_attempts(agent: str, *, db_path: Path | None = None):
    """Every recorded attempt for ``agent``, OLDEST FIRST, as ``(attempt, Relocation)``.

    The audit read. An attempt whose stored JSON will not parse is SKIPPED
    rather than raising — one corrupt row must not hide the rest of the history,
    and the row itself is still there for whoever wants to look at it.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        rows = conn.execute(
            "SELECT attempt, from_host, to_host, phase, steps FROM relocation_journal "
            "WHERE agent=? ORDER BY attempt ASC",
            (agent,),
        ).fetchall()
    out = []
    for row in rows:
        relocation = _relocation_from_row(agent, row[1], row[2], row[3], row[4])
        if relocation is not None:
            out.append((int(row[0]), relocation))
    return tuple(out)


def _relocation_from_row(agent: str, from_host, to_host, phase, steps_json):
    """Rebuild one :class:`Relocation`, or ``None`` when the row will not parse."""
    from .._lifecycle._relocate_phases import Relocation, Step

    try:
        raw = json.loads(steps_json)
        steps = tuple(
            Step(phase=s["phase"], at=float(s["at"]), detail=s.get("detail", ""))
            for s in raw
        )
        return Relocation(
            agent=agent,
            from_host=from_host,
            to_host=to_host,
            phase=phase,
            steps=steps,
        )
    except Exception:  # stx-allow: fallback (reason: an unparseable journal row must not make an agent unrelocatable nor hide the other attempts; the row is kept, not deleted, and the caller opens a fresh relocation)
        return None


def load_journal(agent: str, *, db_path: Path | None = None):
    """The LATEST attempt's relocation for ``agent``, or ``None``.

    The resume read, and the reason it is the latest rather than the only one:
    a re-run continues the attempt that stopped, and the earlier attempts are
    history — present, readable through :func:`load_journal_attempts`, and never
    resumed by accident.

    A stored row whose JSON will not parse returns ``None`` rather than raising:
    the caller's next move is to open a fresh relocation, and a corrupt journal
    must not make the agent unrelocatable. Nothing is deleted — the bad row stays
    for whoever wants to look at it.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT from_host, to_host, phase, steps FROM relocation_journal "
            "WHERE agent=? ORDER BY attempt DESC LIMIT 1",
            (agent,),
        ).fetchone()
    if row is None:
        return None
    return _relocation_from_row(agent, row[0], row[1], row[2], row[3])
