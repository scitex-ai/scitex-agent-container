"""``incarnations`` — the birth certificate (+ death mirror), on PostgreSQL only.

v4 step 5, operator requirement verbatim (card sac-v4-layering-refactor-
harness-runtime-inference-20260813, 2026-08-14): 「起動した後にコンパイル
された最終的なスペックをエージェントが持つようにしてください、この
エージェントはこうして生まれました、という情報です。状態なのでdb に
入れるのがよさそうですよね」 — at launch, record the COMPILED final spec
(post-inheritance, post-defaults) as the agent's birth certificate,
keyed by incarnation id, in the DB.

One record joins the three settled identities:

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

THE STORAGE NOTE THIS MODULE USED TO CARRY IS NOW DISCHARGED
============================================================
The previous docstring said, in as many words, that the birth record went
through the SQLite factory so that "the separately-carded sqlite→Postgres
migration carries it along instead of this PR front-running it". This IS
that migration. The note was a promise; this module is it being kept.

The move is by ADOPTING :mod:`scitex_dev.store` — the fleet's own store
primitive — rather than by sac growing a private psycopg layer, exactly as
:mod:`.state_db_verdict_dedup` did before it. That primitive implements the
operator's 2026-08-19 rule ("fail fast, fail loud, no fallbacks") in its own
``resolve_target``: exactly two steps (``SCITEX_STORE_DSN`` or the per-host
Postgres) and deliberately NO SQLite fallback. A host whose PostgreSQL is
unreachable raises ``StoreTargetError`` naming the DSN it could not reach.

``db_path`` IS GONE from every function. It named a SQLite file; there is no
file. Callers that used to thread it through simply stop.

TWO DELIBERATE DIVERGENCES FROM THE verdict_dedup TEMPLATE
==========================================================
1. TIMESTAMPS STAY ISO-8601 TEXT, not the unix-seconds REAL that the dedup
   tables use. ``born_at``/``exited_at`` are READ BY CALLERS as strings, and
   a birth certificate is an operator-facing artifact. Switching the wire
   format while switching the backend would be a second, silent, breaking
   change riding along inside the first — the kind that shows up months
   later as an unparseable timestamp. Same clock (:func:`state_db.now_iso`),
   same format, different storage.

2. THE BIRTH FIELDS ARE ``LAST_WRITER_WINS``, NOT ``IMMUTABLE``. The SQLite
   version was ``INSERT OR REPLACE`` and its docstring said why: "a retried
   launch that re-records the same id must refresh rather than crash".
   ``IMMUTABLE`` would make that retry raise. This is the opposite call from
   verdict_dedup, where a moved timestamp silently reorders a failure streak
   — there the immutability IS the invariant; here refreshability is.

THE ONE INVARIANT THAT MUST NOT BE LOST
=======================================
``record_incarnation_exit`` returns True iff a birth record existed, and a
missing one is a False, NEVER an insert. The old docstring states the
reason and it survives the port unchanged: "a death with no recorded birth
is a real signal (a pre-artifact incarnation, or a birth write that failed)
and fabricating a birth here would hide it."

The store has no UPDATE-only verb — ``put`` writes whatever record you hand
it — so the SQLite ``UPDATE ... WHERE`` no longer refuses on its own. The
refusal is therefore EXPLICIT here: read first, return False on a miss, and
only then write. Written out rather than relied upon, because the guard
moved from the database into this function and a reader needs to see it.

For the same reason the exit write sends the WHOLE merged record rather than
the three exit fields alone: it does not depend on the store's per-field
merge semantics to preserve the birth data, so the row cannot be hollowed
out by a partial write. The cost is one extra read per exit, on a path that
runs once per process lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "incarnations"

#: Every write from this host is attributed to one node. A birth certificate
#: is written by the launcher ON the host that launched it, and the death
#: mirror by that same incarnation's runner, so SINGLE_WRITER is the honest
#: policy rather than a convenience.
_ACTOR = "scitex-agent-container"

__all__ = [
    "STORE_NAME",
    "get_incarnation",
    "incarnation_store_target",
    "init_incarnations_schema",
    "open_incarnation_store",
    "record_incarnation_birth",
    "record_incarnation_exit",
]


def _schema() -> Any:
    """The birth-certificate schema.

    Built lazily so importing this module does not import scitex-dev; the
    old module was equally lazy about ``state_db``, for the same reason
    (import cost off the hot path).

    ``incarnation_id`` is the sole IDENTITY field and is IMMUTABLE because
    the store enforces it on identities — "changing one does not update the
    record, it names a different record", which is exactly right for a
    process lifetime. Every other field is data ABOUT that lifetime and
    takes ``LAST_WRITER_WINS`` (see the module docstring for why this
    differs from verdict_dedup).

    ``agent_id`` is indexed: the SQLite table carried
    ``idx_incarnations_agent ON (agent_id, born_at)``, and the queries that
    index served — "every incarnation of this agent" — are the ones a
    future reader will write.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def fact(kind: Any, *, required: bool = False, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "incarnation_id": ident(FieldKind.TEXT),
            # --- birth: written once at launch, refreshed by a retry ---
            "agent_id": fact(FieldKind.TEXT, required=True, indexed=True),
            "spec_id": fact(FieldKind.TEXT),
            "spec_git_sha": fact(FieldKind.TEXT, required=True),
            "host": fact(FieldKind.TEXT, required=True),
            "born_at": fact(FieldKind.TEXT, required=True),
            "compiled_spec_json": fact(FieldKind.TEXT, required=True),
            # --- death: absent until the incarnation ends ---
            "exit_reason": fact(FieldKind.TEXT),
            "exit_code": fact(FieldKind.INTEGER),
            "exited_at": fact(FieldKind.TEXT),
        },
    )


def incarnation_store_target() -> Any:
    """Resolve WHERE the birth certificates live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_incarnation_store() -> Store:
    """Open the incarnations store. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function here opens and closes
    one per call, mirroring the old ``with open_db(...)`` shape: births and
    deaths happen once per process lifetime, so the connection cost sits on
    a launch path, never on a request path.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        incarnation_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_incarnations_schema() -> str:
    """Create the incarnations tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL
    equivalent of the ``Path`` the SQLite version returned, and useful in
    exactly the same way: it names WHERE the state actually went, so an
    operator can check it rather than assume it.
    """
    store = open_incarnation_store()
    try:
        return str(incarnation_store_target().locator)
    finally:
        store.close()


def record_incarnation_birth(
    incarnation_id: str,
    *,
    agent_id: str,
    spec_id: str | None,
    spec_git_sha: str,
    host: str | None,
    compiled_spec_json: str,
) -> str:
    """Write the birth certificate for one incarnation. Returns the id.

    Upsert, preserving the old ``INSERT OR REPLACE``: the launch path writes
    exactly once per incarnation, and a retried launch that re-records the
    same id must refresh rather than crash.

    A retry refreshes ``born_at`` too. That matches the SQLite behaviour
    exactly — ``INSERT OR REPLACE`` rewrote the whole row — and is the
    correct reading: the birth being recorded is the one that took.
    """
    from scitex_dev.store import ANY_REVISION

    from .state_db import now_iso
    from .state_db_hostname import resolve_host

    store = open_incarnation_store()
    try:
        store.put(
            {
                "incarnation_id": incarnation_id,
                "agent_id": agent_id,
                "spec_id": spec_id,
                "spec_git_sha": spec_git_sha,
                "host": resolve_host(host),
                "born_at": now_iso(),
                "compiled_spec_json": compiled_spec_json,
            },
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()
    return incarnation_id


def record_incarnation_exit(
    incarnation_id: str,
    *,
    reason: str,
    code: int,
) -> bool:
    """Mirror the terminal ExitRecord onto the incarnation's record.

    Returns True iff a birth record existed to update. A missing record is a
    False, not an insert — a death with no recorded birth is a real signal
    (a pre-artifact incarnation, or a birth write that failed) and
    fabricating a birth here would hide it. Idempotent-by-overwrite: the
    LAST exit write wins, matching ``exit.json`` semantics.

    The read-then-write is not an optimisation and must not be collapsed
    into a bare ``put``: the store has no UPDATE-only verb, so this function
    IS the refusal that the SQLite ``UPDATE ... WHERE`` used to perform.
    """
    from scitex_dev.store import ANY_REVISION

    from .state_db import now_iso

    key = {"incarnation_id": incarnation_id}
    store = open_incarnation_store()
    try:
        existing = store.get(key)
        if existing is None:
            return False
        merged = dict(existing.values)
        merged.update(
            {
                "incarnation_id": incarnation_id,
                "exit_reason": reason,
                "exit_code": int(code),
                "exited_at": now_iso(),
            }
        )
        store.put(merged, expected_revision=ANY_REVISION)
    finally:
        store.close()
    return True


def get_incarnation(incarnation_id: str) -> dict | None:
    """Return one incarnation record as a dict, or None when unknown.

    ``Row.values`` is the schema fields only — the store's own bookkeeping
    (hlc, seq, origin, owner) hangs off sibling attributes and deliberately
    does NOT ride along in the returned dict. The SQLite version returned
    ``dict(sqlite3.Row)``, which was likewise exactly the table columns, so
    callers see the same shape they always did.
    """
    store = open_incarnation_store()
    try:
        row = store.get({"incarnation_id": incarnation_id})
    finally:
        store.close()
    return dict(row.values) if row is not None else None
