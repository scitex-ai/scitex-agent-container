"""comms_grants CRUD — explicit cross-group send permissions (WI-2).

Extracted from :mod:`.state_db_nodes` so that module stays under the
per-file line cap. The four primitives below are re-exported from
``state_db_nodes`` so the existing import surface is unchanged:

    from scitex_agent_container._state.state_db_nodes import (
        grant_send, revoke_send, has_grant, list_comms_grants,
    )

A grant is a directed ``sender → target`` record permitting
``sender → target`` even when the two are in different groups. The
sender identity is the ``metadata.from_agent`` claim, taken at its word
by :func:`scitex_agent_container._listen._acl.check_send_acl` — a
per-node bearer was supposed to pin it, but that feature was removed
2026-08-28 having never been armed. The optional ``note`` is a
free-form audit annotation.

ON POSTGRESQL SINCE 2026-08-28 (the operator's storage-consolidation
order). The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO local-file
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach.

THE SCHEMA AND THE CONNECTION MOVED TO :mod:`.state_db_grants_store` on
2026-08-29, when the rename step below gained a sibling module and this
one would otherwise have grown past the per-file cap. That split also
closed the asymmetry the port had left behind: grants was the ONLY
ACL-path store still opening a fresh connection per call with no
reconnect wrapper. Every verb here now runs through
``run_with_reconnect`` on the shared handle. ``_open`` stays importable
from this module and keeps meaning exactly what it meant — a FRESH,
caller-owned ``Store`` the caller closes.

``db_path`` IS GONE from all four signatures. It named a file;
there is no file. Test isolation now comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema — the ``pg_schema`` fixture
— which is a better isolation than a temp path was, because it
exercises the real resolver.

THREE THINGS THIS MIGRATION HAD TO PRESERVE, each a real property of
the previous implementation rather than an incidental behaviour:

1. REVOKE IS NOT A DELETE ANY MORE, and that is a strengthening. The
   original issued ``DELETE FROM comms_grants``, so a revoked
   grant left no trace and "was never granted" and "was granted then
   revoked" became indistinguishable — for an ACL table that is the
   difference between a clean history and an unanswerable audit
   question. ``Store.hide`` is the store's only removal: the row, its
   values and its whole history stay readable through
   ``include_hidden=True`` and in the oplog, while every default read
   treats it as absent. :func:`has_grant` therefore still returns
   ``False`` immediately after a revoke — the security behaviour is
   unchanged; only the forgetting stopped.

2. THE LISTING ORDER IS THE HLC, NOT ``created_at``. The original
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

import time
from typing import TYPE_CHECKING, Any

from .state_db_grants_store import (
    ACTOR,
    GRANTS_STORE,
    grant_as_dict,
    grants_schema,
    hlc_sort_key,
    new_grants_store,
    run_with_reconnect,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "GRANTS_STORE",
    "grant_send",
    "has_grant",
    "list_comms_grants",
    "revoke_send",
]


def _open() -> "Store":
    """A FRESH, caller-owned grants store. The CALLER must ``close()`` it.

    Kept under this name and with this meaning because three tests and
    ``scripts/migrate_comms_grants_to_postgres.py`` import it from here and
    every one of them closes what it is handed. Returning the process-wide
    handle instead would make each of those closes break the ACL reads that
    share it — so the shared handle is reached only through
    ``run_with_reconnect``, which this module's own verbs use.
    """
    return new_grants_store()


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

    def _grant(store: "Store") -> None:
        key = {"sender_name": sender, "target_name": target}
        # include_hidden: a revoked row still occupies the identity, so
        # a plain read would say "absent" and the insert would collide.
        existing = store.get(key, include_hidden=True)
        if existing is not None:
            if store.is_hidden(key):
                store.unhide(key, expected_revision=ANY_REVISION, actor=ACTOR)
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

    run_with_reconnect(_grant)


def revoke_send(*, sender: str, target: str) -> bool:
    """Withdraw a ``sender → target`` grant. ``True`` iff one was live.

    Hides rather than deletes (see the module docstring): the grant
    stops authorising immediately, and the fact that it once existed
    stays auditable.
    """
    if not sender or not target:
        return False

    from scitex_dev.store import ANY_REVISION

    def _revoke(store: "Store") -> bool:
        key = {"sender_name": sender, "target_name": target}
        if store.get(key) is None:
            # Absent, or already hidden — either way nothing was live,
            # which is what a zero rowcount meant.
            return False
        store.hide(key, expected_revision=ANY_REVISION, actor=ACTOR)
        return True

    return bool(run_with_reconnect(_revoke))


def has_grant(*, sender: str, target: str) -> bool:
    """Return ``True`` iff a LIVE ``sender → target`` grant exists.

    The security predicate. A hidden (revoked) record reads as absent
    here, so a revoke denies immediately.
    """
    if not sender or not target:
        return False

    return bool(
        run_with_reconnect(
            lambda store: store.get(
                {"sender_name": sender, "target_name": target}
            )
            is not None
        )
    )


def list_comms_grants() -> list[dict[str, Any]]:
    """Every LIVE grant, in causal insertion order.

    Observability surface for the host operator. Each row carries the
    audit ``note``.

    Ordered by the hybrid logical clock — see the module docstring for
    why NOT ``created_at``. Revoked grants are omitted, matching what
    the DELETE-based version showed; their history remains in the
    oplog for anyone auditing what changed.
    """
    # rows() excludes hidden by default, which IS the revoked-grant
    # filter — spelled out because the exclusion is load-bearing here.
    rows = list(run_with_reconnect(lambda store: store.rows()))
    rows.sort(key=hlc_sort_key)
    return [grant_as_dict(row) for row in rows]


# Kept importable from here because the schema is part of what a caller
# reaching for ``_open`` is reaching for; the definition itself now lives in
# the store module beside the connection it describes.
_grants_schema = grants_schema

# EOF
