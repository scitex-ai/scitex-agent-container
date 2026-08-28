"""comms_grants CRUD — explicit cross-group send permissions (WI-2).

Extracted from :mod:`.state_db_nodes` so that module stays under the
per-file line cap. The four primitives below are re-exported from
``state_db_nodes`` so the existing import surface is unchanged:

    from scitex_agent_container._state.state_db_nodes import (
        grant_send, revoke_send, has_grant, list_comms_grants,
    )

A grant is a directed ``sender → target`` record permitting
``sender → target`` even when the two are in different groups. The
sender identity is authenticated by
:class:`scitex_agent_container._listen._acl.NodeAuthMiddleware`
resolving the bearer; the optional ``note`` is a free-form audit
annotation.

ON POSTGRESQL SINCE 2026-08-28 (the operator's SQLite-eradication
order). The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO SQLite
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach.

``db_path`` IS GONE from all four signatures. It named a SQLite file;
there is no file. Test isolation now comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema — the ``pg_schema`` fixture
— which is a better isolation than a temp path was, because it
exercises the real resolver.

THREE THINGS THIS MIGRATION HAD TO PRESERVE, each a real property of
the SQLite version rather than an incidental behaviour:

1. REVOKE IS NOT A DELETE ANY MORE, and that is a strengthening. The
   SQLite version issued ``DELETE FROM comms_grants``, so a revoked
   grant left no trace and "was never granted" and "was granted then
   revoked" became indistinguishable — for an ACL table that is the
   difference between a clean history and an unanswerable audit
   question. ``Store.hide`` is the store's only removal: the row, its
   values and its whole history stay readable through
   ``include_hidden=True`` and in the oplog, while every default read
   treats it as absent. :func:`has_grant` therefore still returns
   ``False`` immediately after a revoke — the security behaviour is
   unchanged; only the forgetting stopped.

2. THE LISTING ORDER IS THE HLC, NOT ``created_at``. The SQLite
   docstring recorded, at length, why it ordered by ``rowid``: a
   wall-clock ``created_at`` ties on bulk-imported peer rows and skews
   across hosts, so a foreign row sorted into a plausible-looking
   position instead of standing out — which is what let a leaked
   ``-> lead`` grant hide inside the listing. ``rowid`` does not exist
   here. Its correct successor is the hybrid logical clock, which is
   built for exactly this: monotonic per origin, causally ordered
   across origins, and immune to clock skew. Ordering by ``created_at``
   would reintroduce the original bug verbatim.

3. RE-GRANTING STAYS IDEMPOTENT. A re-grant of a live pair leaves the
   row untouched and does not bump the timestamp, as before. A
   re-grant of a REVOKED pair un-hides it — that case could not arise
   under DELETE, and treating it as a no-op would leave the operator
   unable to restore a grant they had just revoked.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "GRANTS_STORE",
    "grant_send",
    "has_grant",
    "list_comms_grants",
    "revoke_send",
]

#: Logical store name. Renders as four physical tables
#: (``comms_grants_rows``, ``_oplog``, ``_identity``, ``_cursor``).
GRANTS_STORE = "comms_grants"

_ACTOR = "scitex-agent-container"


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any, *, required: bool = False) -> Any:
    """A granted permission is a historical fact — IMMUTABLE.

    ``created_at`` records WHEN the permission was given; a merge that
    could move it would rewrite the audit trail an operator reads to
    answer "since when could this agent send there?". The same applies
    to ``note``, which names the authorisation.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _grants_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=GRANTS_STORE,
        fields={
            # The directed pair IS the identity, exactly as the SQLite
            # (sender_name, target_name) lookup treated it.
            "sender_name": _ident(FieldKind.TEXT),
            "target_name": _ident(FieldKind.TEXT),
            "created_at": _fact(FieldKind.REAL, required=True),
            "note": _fact(FieldKind.TEXT),
        },
    )


def _open() -> "Store":
    """Open the grants store. RAISES if PostgreSQL is unreachable.

    MULTI_WRITER, deliberately. A grant's record has no single stable
    owner: it is created on one host, revoked by an operator from
    another, and bulk-imported from peers by ``state_db_export``.
    Under SINGLE_WRITER the first revoke-from-elsewhere would be an
    illegal write — the same reasoning the store's own policy
    documentation gives for the card store.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = _grants_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )


def _hlc_sort_key(row: Any) -> tuple:
    """Total order over records, immune to wall-clock skew.

    The successor to the SQLite ``rowid`` ordering. ``node`` is the
    final tiebreak so the order is total rather than merely partial —
    two origins can mint the same (wall_us, logical) pair.
    """
    hlc = row.hlc
    return (hlc.wall_us, hlc.logical, hlc.node)


def grant_send(
    *,
    sender: str,
    target: str,
    note: str | None = None,
) -> None:
    """Insert (or restore) a cross-group grant ``sender → target``.

    Idempotent — re-granting a LIVE pair leaves the row untouched and
    does not bump the timestamp. Re-granting a REVOKED pair un-hides
    it, which is the case DELETE could not express.
    """
    if not sender or not target:
        raise ValueError("grant_send: sender and target must be non-empty")

    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    store = _open()
    try:
        key = {"sender_name": sender, "target_name": target}
        # include_hidden: a revoked row still occupies the identity, so
        # a plain read would say "absent" and the insert would collide.
        existing = store.get(key, include_hidden=True)
        if existing is not None:
            if store.is_hidden(key):
                store.unhide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
            return
        store.put(
            {
                "sender_name": sender,
                "target_name": target,
                "created_at": time.time(),
                "note": note,
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()


def revoke_send(*, sender: str, target: str) -> bool:
    """Withdraw a ``sender → target`` grant. ``True`` iff one was live.

    Hides rather than deletes (see the module docstring): the grant
    stops authorising immediately, and the fact that it once existed
    stays auditable.
    """
    if not sender or not target:
        return False

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        key = {"sender_name": sender, "target_name": target}
        if store.get(key) is None:
            # Absent, or already hidden — either way nothing was live,
            # which is what the SQLite rowcount==0 meant.
            return False
        store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        return True
    finally:
        store.close()


def has_grant(*, sender: str, target: str) -> bool:
    """Return ``True`` iff a LIVE ``sender → target`` grant exists.

    The security predicate. A hidden (revoked) record reads as absent
    here, so a revoke denies immediately.
    """
    if not sender or not target:
        return False

    store = _open()
    try:
        return store.get({"sender_name": sender, "target_name": target}) is not None
    finally:
        store.close()


def list_comms_grants() -> list[dict[str, Any]]:
    """Every LIVE grant, in causal insertion order.

    Observability surface for the host operator. Each row carries the
    audit ``note``.

    Ordered by the hybrid logical clock — see the module docstring for
    why NOT ``created_at``. Revoked grants are omitted, matching what
    the DELETE-based version showed; their history remains in the
    oplog for anyone auditing what changed.
    """
    store = _open()
    try:
        # rows() excludes hidden by default, which IS the revoked-grant
        # filter — spelled out because the exclusion is load-bearing here.
        rows = list(store.rows())
        rows.sort(key=_hlc_sort_key)
        return [
            {
                "sender": str(row.values["sender_name"]),
                "target": str(row.values["target_name"]),
                "created_at": float(row.values["created_at"]),
                "note": row.values.get("note"),
            }
            for row in rows
        ]
    finally:
        store.close()
