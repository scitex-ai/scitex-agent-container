"""``incarnations`` — the birth certificate (+ death mirror) table.

v4 step 5, operator requirement verbatim (card sac-v4-layering-refactor-
harness-runtime-inference-20260813, 2026-08-14): 「起動した後にコンパイル
された最終的なスペックをエージェントが持つようにしてください、この
エージェントはこうして生まれました、という情報です。状態なのでdb に
入れるのがよさそうですよね」 — at launch, record the COMPILED final spec
(post-inheritance, post-defaults) as the agent's birth certificate,
keyed by incarnation id, in the DB.

One row joins the three settled identities:

  * ``incarnation_id`` — one process lifetime (== ``instances.id``; the
    beat and the ExitRecord carry the same key);
  * ``agent_id``       — the durable named subject;
  * ``spec_id`` + ``spec_git_sha`` — the design document and the exact
    git commit it was compiled from (``"unresolvable"`` recorded
    honestly when the spec dir is not a git repo on this host).

``compiled_spec_json`` is the fully-resolved :class:`AgentConfig`
serialized WITH SECRETS REDACTED — credentials are referenced by
slot/source name (account slug, env-var NAME, credentials-file PATH),
never by value (see ``_lifecycle._birth_certificate``).

STORAGE NOTE (stated deliberately): the fleet target for running state
is per-host Postgres :55432 (operator ruling), but sac's state layer
today is this sqlite ``state.db`` behind :func:`state_db.open_db`'s
central factory. The birth record therefore goes through the EXISTING
factory — a new table beside ``instances``/``heartbeats`` — so the
separately-carded sqlite→Postgres migration carries it along instead of
this PR front-running it.

Sibling-module convention (``state_db_diary`` / ``state_db_instances``):
``state_db.init_schema`` runs :data:`_SCHEMA_INCARNATIONS`; the helpers
here import ``open_db`` lazily to stay cycle-free.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "get_incarnation",
    "record_incarnation_birth",
    "record_incarnation_exit",
]

_SCHEMA_INCARNATIONS = """
CREATE TABLE IF NOT EXISTS incarnations (
    incarnation_id      TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    spec_id             TEXT,
    spec_git_sha        TEXT NOT NULL,
    host                TEXT NOT NULL,
    born_at             TEXT NOT NULL,
    compiled_spec_json  TEXT NOT NULL,
    exit_reason         TEXT,
    exit_code           INTEGER,
    exited_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_incarnations_agent
    ON incarnations(agent_id, born_at);
"""


def record_incarnation_birth(
    incarnation_id: str,
    *,
    agent_id: str,
    spec_id: str | None,
    spec_git_sha: str,
    host: str | None,
    compiled_spec_json: str,
    db_path: Path | None = None,
) -> str:
    """Insert the birth certificate row for one incarnation.

    ``INSERT OR REPLACE`` on the primary key: the launch path writes
    exactly once per incarnation, and a retried launch that re-records
    the same id must refresh rather than crash. Returns the id.
    """
    from .state_db import now_iso, open_db
    from .state_db_hostname import resolve_host

    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO incarnations (
                incarnation_id, agent_id, spec_id, spec_git_sha,
                host, born_at, compiled_spec_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incarnation_id,
                agent_id,
                spec_id,
                spec_git_sha,
                resolve_host(host),
                now_iso(),
                compiled_spec_json,
            ),
        )
    return incarnation_id


def record_incarnation_exit(
    incarnation_id: str,
    *,
    reason: str,
    code: int,
    db_path: Path | None = None,
) -> bool:
    """Mirror the terminal ExitRecord onto the incarnation's row.

    Returns True iff a birth row existed to update. A missing row is a
    False, not an insert — a death with no recorded birth is a real
    signal (a pre-artifact incarnation, or a birth write that failed)
    and fabricating a birth here would hide it. Idempotent-by-overwrite:
    the LAST exit write wins, matching ``exit.json`` semantics.
    """
    from .state_db import now_iso, open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE incarnations SET exit_reason=?, exit_code=?, exited_at=? "
            "WHERE incarnation_id=?",
            (reason, int(code), now_iso(), incarnation_id),
        )
        return cur.rowcount > 0


def get_incarnation(
    incarnation_id: str,
    db_path: Path | None = None,
) -> dict | None:
    """Return one incarnation row as a dict, or None when unknown."""
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM incarnations WHERE incarnation_id=?",
            (incarnation_id,),
        ).fetchone()
        return dict(row) if row is not None else None
