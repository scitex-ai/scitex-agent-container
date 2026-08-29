"""Block-list — receiver-driven persistent silencing of a sender, on PostgreSQL.

Operator-requested via lead (2026-06-01). Lead's design amendment: the ACL
approve-prompt flow boils down to BLOCK / UNBLOCK as the primitive operations,
dropping the held-message + TTL + debounce-heuristic machinery.

* UNBLOCK is the existing :func:`state_db_nodes.grant_send` — writes the
  ``comms_grants`` row that lets the sender's future messages pass.
* BLOCK is this module's :func:`block_send` — records a block that makes the
  sender's future ``message:send`` attempts SILENTLY drop (no 403 trail, no
  receiver push, no approve-prompt re-fire). The receiver chose to silence the
  sender; the system honours that without further surface area.

Symmetric helpers (``unblock_send``, ``has_block``) mirror the ``grant_send`` /
``revoke_send`` / ``has_grant`` shape in ``state_db_nodes``.

Block precedence: a (sender, target) pair with BOTH a grant and a block is
denied (block wins). The receiver explicitly silenced the sender after some
earlier grant — honouring the more recent veto.
:func:`_listen._acl.check_send_acl` enforces this precedence.

WHY THIS MODULE NO LONGER TOUCHES SQLite
========================================
The operator's 2026-08-19 order was to eradicate SQLite and move to PostgreSQL:
"fail fast, fail loud, no fallbacks". This is the fourth table to move, after
``verdict_delivered``, ``incarnations`` and ``pending_prompts``, and it moves the
same way — by ADOPTING :mod:`scitex_dev.store` rather than by sac growing a
private psycopg layer.

``db_path`` IS GONE from every function. It named a SQLite file; there is no
file. ``grant_flush`` and ``_listen._acl`` stop threading one in.

NO MIGRATION SCRIPT, and that is measured rather than assumed. When this slice
was scoped the prediction was the opposite — "``comms_blocks`` is a DURABLE
decision, not a transient flag, so it almost certainly holds real rows and this
one DOES need a migration". It holds ZERO rows: 52 SQLite databases read across
compute-01..04 (the fleet state.db plus every per-agent shard), 0 in all of them.
Nobody has ever blocked anyone. The prediction was reasonable and wrong, which is
why it was checked before any code was written.

A CONSEQUENCE WORTH STATING RATHER THAN DISCOVERING
===================================================
``has_block`` is the FIRST gate in :func:`_listen._acl.check_send_acl`, so this
table is now read on every message send, and an unreachable PostgreSQL makes
that read RAISE. Message delivery therefore depends on the per-host store being
up, where before it depended only on a local file.

That is the intended direction, not an oversight. The operator's ruling on this
migration was "fail fast, fail loud, no fallbacks", and the alternative here is
worse than downtime: a block check that cannot reach its store and answers
"not blocked" silently delivers to a receiver who explicitly silenced the
sender. There is no safe default for this question, which is exactly why it
must not have one.

The connection cost is unchanged in SHAPE — the SQLite version also opened and
closed a connection per check — but a unix-socket round trip is dearer than a
file open. If that ever shows up in send latency, the fix is a pooled or
long-lived store handle, not a cache of the answer: a cached block is a block
that keeps working after it was lifted.

THE CLEARED-IS-NOT-ABSENT LIFECYCLE, AND WHERE IT DIFFERS FROM pending_prompts
==============================================================================
Like the pending-prompt flag, this table DELETEs and the store has no delete —
it has ``hide``, which marks a record invisible to ``get``/``rows`` while keeping
it in the oplog. That is the better primitive here for the same reason: who
unblocked whom, and when, survives instead of being destroyed.

So the same three-value branch applies, and :meth:`Store.is_hidden` is what makes
it expressible, because it answers in THREE values where ``get`` answers in two:

    None    no record at all      -> insert; this is a new block
    True    unblocked earlier     -> unhide, and stamp a NEW created_at
    False   present and visible   -> already blocked; leave it untouched

The DIFFERENCE from ``pending_prompts`` is the middle and last lines, and it
comes from this module's own documented idempotence: "re-blocking the same pair
leaves the row untouched (timestamp not bumped)". So the visible branch writes
NOTHING — not even a refreshed timestamp — while the hidden branch writes a new
one, because an unblock followed by a block is a NEW decision by the receiver
and dating it to the superseded block would misreport when they made it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "comms_blocks"

#: Every write from this host is attributed to one node. A block is written by
#: the listen daemon that served the receiver's decision and cleared by the same
#: host's unblock path, so SINGLE_WRITER is honest rather than convenient.
_ACTOR = "scitex-agent-container"

__all__ = [
    "STORE_NAME",
    "block_send",
    "ensure_comms_blocks_table",
    "has_block",
    "open_blocks_store",
    "blocks_store_target",
    "unblock_send",
]


def _schema() -> Any:
    """The block-list schema.

    Built lazily so importing this module does not import scitex-dev; the old
    module was equally lazy about ``state_db``, for the same reason.

    ``(sender_name, target_name)`` is the composite IDENTITY — the SQLite
    table's PRIMARY KEY, unchanged. Identity fields must be IMMUTABLE and the
    store enforces it: "changing one does not update the record, it names a
    different record", which is exactly right for a pair.

    ``created_at`` is LAST_WRITER_WINS rather than IMMUTABLE, and that is load
    bearing for exactly one path: a pair that was unblocked and then blocked
    again must carry the time of the NEW decision. The repeat-block path never
    writes, so the "timestamp not bumped" guarantee is preserved by the caller's
    control flow rather than by the merge rule.

    ``note`` is optional — a free-form audit annotation (e.g. the prompt msg_id
    the receiver was responding to), and ``grant_flush.block_and_clear`` passes
    one. It is NOT required, because the CLI path blocks without one.
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

    def fact(kind: Any, *, required: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=False,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "sender_name": ident(FieldKind.TEXT),
            "target_name": ident(FieldKind.TEXT),
            "created_at": fact(FieldKind.REAL, required=True),
            "note": fact(FieldKind.TEXT),
        },
    )


def blocks_store_target() -> Any:
    """Resolve WHERE the block list lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_blocks_store() -> Store:
    """Open the block store. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function opens and closes one per
    call, mirroring the old ``with open_db(...)`` shape: ``has_block`` runs on
    the ACL path, but that path already crosses a process boundary, and a
    connection per check is what the SQLite version did too.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        blocks_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def ensure_comms_blocks_table() -> str:
    """Create the block-list tables if missing. Idempotent.

    Kept under its original name because :func:`_state.state_db.init_schema`
    calls it by that name and the name still says what it does.

    Returns the resolved store LOCATOR as a string — the PostgreSQL equivalent
    of the ``None`` the SQLite version returned, and strictly more useful: it
    names WHERE the state actually went, so an operator can check it rather than
    assume it.
    """
    store = open_blocks_store()
    try:
        return str(blocks_store_target().locator)
    finally:
        store.close()


def block_send(*, sender: str, target: str, note: str | None = None) -> None:
    """Persist a ``sender → target`` block.

    Idempotent — re-blocking the same pair leaves the record untouched
    (timestamp not bumped, note not replaced). Blocking a pair that was
    UNBLOCKED earlier is not a repeat: it is a new decision, and it is stamped
    with the time it was made.

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError``.

    CONCURRENCY. The insert carries ``expected_revision=NEW_RECORD``, so a
    racing writer that created the record between our check and our write raises
    ``RevisionMismatchError`` — which means "someone else blocked this pair",
    the same end state this call wanted. Nothing else is caught: an unreachable
    store must still be loud.
    """
    if not sender or not target:
        raise ValueError("block_send: sender and target must be non-empty")

    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    key = {"sender_name": sender, "target_name": target}
    store = open_blocks_store()
    try:
        hidden = store.is_hidden(key)
        if hidden is False:
            return
        record = {**key, "created_at": time.time(), "note": note}
        if hidden is True:
            store.unhide(key, expected_revision=ANY_REVISION)
            store.put(record, expected_revision=ANY_REVISION)
            return
        try:
            store.put(record, expected_revision=NEW_RECORD)
        except RevisionMismatchError:
            return
    finally:
        store.close()


def unblock_send(*, sender: str, target: str) -> bool:
    """Remove a ``sender → target`` block. Returns ``True`` iff one was removed.

    ``unblock_send`` only clears the block — it does NOT write a grant. The
    receiver-side decision flow is "unblock = grant + clear pending"; the
    CLI/handler stitches the two calls together (see
    ``cli_pkg/a2a_group.py::a2a_unblock``).

    HIDE RATHER THAN DELETE. The store has no delete verb, and that is the
    better fit: the unblock stays in the oplog with the actor that made it, so
    "who let this sender back in, and when" survives — which a DELETE destroyed.
    The read side is unchanged because ``get`` skips hidden records.
    """
    if not sender or not target:
        return False

    from scitex_dev.store import ANY_REVISION

    key = {"sender_name": sender, "target_name": target}
    store = open_blocks_store()
    try:
        if store.get(key) is None:
            return False
        store.hide(key, expected_revision=ANY_REVISION)
        return True
    finally:
        store.close()


def has_block(*, sender: str, target: str) -> bool:
    """Return True iff ``(sender → target)`` is currently blocked.

    Used by :func:`_listen._acl.check_send_acl` as the FIRST gate after the
    trivial self-send / phase-3 checks. A blocked sender is silently dropped —
    no 403 reason, no receiver push, no approve-prompt re-fire.

    ``get`` excludes hidden records by default, so an unblocked pair reads as
    absent here — which is precisely the SQLite DELETE semantics this replaces.
    """
    if not sender or not target:
        return False
    store = open_blocks_store()
    try:
        return store.get({"sender_name": sender, "target_name": target}) is not None
    finally:
        store.close()
