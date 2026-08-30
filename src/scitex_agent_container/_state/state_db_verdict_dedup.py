"""CI-verdict delivery dedup — the "delivered-set", on PostgreSQL only.

sac polls GitHub CI on its own schedule and a2a-delivers each verdict to the
pusher EXACTLY ONCE, deduping on ``(repo, pr, head_sha, conclusion)`` so a
re-poll or a ``sac listen`` restart never re-delivers a verdict the agent
already saw. A re-run that flips the conclusion on the same ``head_sha`` is a
DISTINCT key, so the flipped verdict IS delivered; a new push is likewise
distinct. The dedup is per exact outcome, not per PR.

WHY THIS MODULE IS ON THE SHARED STORE
========================================
The operator's 2026-08-19 order was to move every table to
PostgreSQL: "fail fast, fail loud, no fallbacks". This is the first table to
move, and it moves by ADOPTING :mod:`scitex_dev.store` — the fleet's own
store primitive — rather than by sac growing a private psycopg layer.

That primitive already implements the operator's rule, in its own words at
``resolve_target``: "exactly two steps (``SCITEX_STORE_DSN`` or the per-host
Postgres) and deliberately no local-file fallback: a host whose Postgres is down
must fail loudly rather than start writing to a private local file that
shares nothing."

So there is nothing here to fall back TO. A host whose PostgreSQL is
unreachable raises ``StoreTargetError`` naming the DSN it could not reach.
That is the intended behaviour: a verdict that cannot be recorded must not
be silently re-delivered forever, and a dedup set written to a file nobody
reads is worse than no dedup at all.

WHAT REPLACED WHAT
==================
``db_path`` IS GONE from every function. It named a file; there is no
file. The store target comes from :func:`scitex_dev.store.host_store`, which
sac's containers reach because ``SCITEX_STORE_DSN`` is injected as a fleet
default (see :mod:`.._fleet_env`). Callers that used to thread ``db_path``
through simply stop.

``failures_since_last_success`` was a correlated SQL aggregate. The store
exposes ``get``/``put``/``rows``, not SQL, so the streak is now computed in
Python. THE COST IS STATED RATHER THAN HIDDEN: ``rows()`` materialises the
whole delivered-set, where the old query counted server-side. Measured
2026-08-19 the largest host held 722 rows and the fleet 1,298, growing by a
handful a day, so this is comfortable for years — but it is O(n) per call
and if this table ever reaches six figures the streak wants an indexed
query instead. Recorded so a future reader finds a decision, not a surprise.

Writing is INSERT-OR-IGNORE, preserved exactly: a re-seen verdict must not
move ``delivered_at``, because that timestamp is what ORDERS the streak. A
re-poll that refreshed it would silently reorder history and the failure cap
would stop capping, with no error anywhere.

TWO INDEPENDENT GUARDS HOLD THAT INVARIANT, and I only learned there were
two by trying to break it:

  1. ``put`` is attempted only for a key that is ABSENT, with
     ``expected_revision=NEW_RECORD``. A concurrent writer that wins the
     race raises ``RevisionMismatchError``, which means "already recorded"
     — the same outcome the old ``INSERT OR IGNORE`` produced. No other
     exception is caught; an unreachable store must still be loud.
  2. the DATA fields are ``MergeRule.IMMUTABLE`` in the schema, so the
     store itself REFUSES a later write to them.

Measured 2026-08-19, one arm at a time: breaking (1) alone (upsert with
``ANY_REVISION``) leaves the invariant intact because (2) catches it, and
breaking (2) alone (``LAST_WRITER_WINS``) leaves it intact because (1)
catches it. Only with BOTH removed does the timestamp move, and the test
then fails alone. So neither guard is redundant decoration and neither is
load-bearing by itself — remove one and the test still passes, which is
precisely why this note exists rather than a comment saying "idempotent".

All times are unix-seconds (float), matching the diary tables.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "verdict_delivered"

#: Every write from this host is attributed to one node. The dedup set is
#: single-writer per host by construction — only that host's ``sac listen``
#: polls CI for it — so SINGLE_WRITER is the honest policy rather than a
#: convenience.
_ACTOR = "scitex-agent-container"


def _schema() -> Any:
    """The delivered-set schema.

    Built lazily so importing this module does not import scitex-dev; the
    old module was equally lazy about ``state_db``, for the same reason
    (import cost off the hot path).

    IDENTITY fields must be IMMUTABLE — the store enforces it, and the
    reason is worth keeping: "changing one does not update the record, it
    names a different record." The two DATA fields are IMMUTABLE too,
    deliberately: a delivered verdict is a historical fact, and a merge that
    could move ``delivered_at`` would silently reorder the failure streak.
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

    def fact(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=False,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "repo": ident(FieldKind.TEXT),
            "pr": ident(FieldKind.INTEGER),
            "head_sha": ident(FieldKind.TEXT),
            "conclusion": ident(FieldKind.TEXT),
            "dispatch_id": fact(FieldKind.TEXT),
            "delivered_at": fact(FieldKind.REAL),
        },
    )


def verdict_store_target() -> Any:
    """Resolve WHERE the delivered-set lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_verdict_store() -> Store:
    """Open the delivered-set store. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function here opens and closes
    one per call, which mirrors the old ``with open_db(...)`` shape — the
    connection cost was acceptable before and the call rate has not changed
    (one CI poll tick, not a request path).
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        verdict_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_verdict_dedup_schema() -> str:
    """Create the delivered-set tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL
    equivalent of the ``Path`` the previous implementation returned, and useful in
    exactly the same way: it names WHERE the state actually went, so an
    operator can check it rather than assume it.

    Opening the store is what creates the tables, so this connects. It is
    the locator, NOT ``Store.identity`` — that is a property, and reaching
    for it with a ``hasattr`` guard would have turned a typo into a silently
    wrong return value instead of an error.
    """
    store = open_verdict_store()
    try:
        return str(verdict_store_target().locator)
    finally:
        store.close()


def verdict_already_delivered(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    conclusion: str,
) -> bool:
    """Return ``True`` iff this exact verdict was already delivered.

    The dedup key is the 4-tuple ``(repo, pr, head_sha, conclusion)``. A miss
    — or a brand-new store — returns ``False`` so the caller delivers.
    """
    store = open_verdict_store()
    try:
        row = store.get(
            {
                "repo": repo,
                "pr": int(pr),
                "head_sha": head_sha,
                "conclusion": conclusion,
            }
        )
    finally:
        store.close()
    return row is not None


def record_verdict_delivered(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    conclusion: str,
    dispatch_id: str | None = None,
    delivered_at: float | None = None,
) -> None:
    """Mark this verdict delivered. Idempotent (re-poll-safe).

    Re-seeing a verdict is a NO-OP rather than an overwrite: ``delivered_at``
    orders the failure streak, so a re-poll that refreshed it would quietly
    reorder history. This is the old ``INSERT OR IGNORE`` semantics, kept.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    key = {
        "repo": repo,
        "pr": int(pr),
        "head_sha": head_sha,
        "conclusion": conclusion,
    }
    ts = float(delivered_at) if delivered_at is not None else time.time()
    store = open_verdict_store()
    try:
        if store.get(key) is not None:
            return
        try:
            store.put(
                {**key, "dispatch_id": dispatch_id, "delivered_at": ts},
                expected_revision=NEW_RECORD,
            )
        except RevisionMismatchError:
            # A concurrent writer created the same key between our get and
            # our put. "Already recorded" is the outcome we wanted; this is
            # the optimistic-concurrency contract, not a swallowed error.
            # Deliberately narrow: nothing else is caught, so an unreachable
            # store still raises.
            return
    finally:
        store.close()


def failures_since_last_success(*, repo: str, pr: int) -> int:
    """Count failure verdicts delivered for this PR since its last green.

    The dedup key includes ``head_sha``, so a PR whose head keeps moving
    re-fires forever — every push is a fresh key. That is correct for a
    branch someone is pushing fixes to, and wrong for a standing sync PR
    whose head tracks its source branch: there, each unrelated merge moves
    the head and earns another "fix-and-push" the recipient cannot act on.
    This count is the streak the caller caps on.

    Counts only rows NEWER than the most recent ``success`` for the same
    ``(repo, pr)``, so a red -> green -> red sequence starts over rather than
    staying capped forever. With no success on record the floor is ``0.0``,
    which every real ``delivered_at`` exceeds.

    Returns 0 on a brand-new store.
    """
    store = open_verdict_store()
    try:
        rows: list[Row] = store.rows()
    finally:
        store.close()

    mine = [
        r.values
        for r in rows
        if r.values.get("repo") == repo and int(r.values.get("pr", -1)) == int(pr)
    ]
    last_success = max(
        (
            float(v.get("delivered_at") or 0.0)
            for v in mine
            if v.get("conclusion") == "success"
        ),
        default=0.0,
    )
    return sum(
        1
        for v in mine
        if v.get("conclusion") == "failure"
        and float(v.get("delivered_at") or 0.0) > last_success
    )


__all__ = [
    "STORE_NAME",
    "verdict_store_target",
    "failures_since_last_success",
    "init_verdict_dedup_schema",
    "open_verdict_store",
    "record_verdict_delivered",
    "verdict_already_delivered",
]
