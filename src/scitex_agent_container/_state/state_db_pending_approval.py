"""Pending-prompt flag for the ACL block/unblock flow — on PostgreSQL only.

Operator-requested via lead (2026-06-01). Lead's design amendment
SUPERSEDED an earlier "hold the original message + replay on grant"
design in favour of a simpler BLOCK / UNBLOCK primitive:

* When a cross-group sender is denied, the receiver gets ONE push
  prompt naming the sender (content hidden — no leak pre-decision).
  The prompt embeds BOTH ``sac a2a unblock <s> <t>`` and
  ``sac a2a block <s> <t>`` so the receiver picks the verb.
* While a (sender, target) pair has a pending prompt, subsequent
  denied attempts from the same sender DO NOT re-prompt — the
  receiver already has the decision in front of them. This module
  owns that "is there a pending row" flag.
* The receiver's decision (either ``unblock`` → ``grant_send`` or
  ``block`` → ``block_send``) clears the pending row. No held message
  replay — if the sender wants their message delivered after unblock,
  they resend.
* No TTL, no expiry sweep, no latest-wins dedupe. The primitive is
  just "is there a pending decision yes/no".

The original message content is NEVER stored here (the receiver
decides on identity, not on message content).

WHY THIS MODULE IS ON THE SHARED STORE
======================================
The operator's 2026-08-19 order was to move every table to PostgreSQL:
"fail fast, fail loud, no fallbacks". This is the third table
to move, after ``verdict_delivered`` and ``incarnations``, and it moves
the same way — by ADOPTING :mod:`scitex_dev.store` rather than by sac
growing a private psycopg layer.

``db_path`` IS GONE from every function. It named a file; there is
no file. ``grant_flush`` was threading its own path in here and
stops; the two records now live in two different databases, which is what
the migration is doing one table at a time.

THE DELETE IS THE NEW GROUND, AND IT IS WHY THIS ONE NEEDED THOUGHT
===================================================================
The two tables that moved before this one only ever inserted. This one
DELETES, and the store has no delete — it has ``hide``, which marks a
record invisible to ``get``/``rows`` while keeping it in the oplog.

That is the better primitive here (the decision history stays auditable),
but it introduces a lifecycle a plain DELETE did not have: a cleared
pair is not ABSENT, it is HIDDEN. Written naively, the next denial for
that pair would find ``get() -> None``, try to insert, collide with the
hidden record, and return "already pending" — so the receiver would never
be prompted again. A fix for a silent-success bug that silently suppresses
prompts forever is worse than the bug.

:meth:`Store.is_hidden` is what makes this expressible, because it answers
in THREE values rather than two:

    None    no record at all          -> insert, this is the first prompt
    True    cleared earlier           -> unhide + refresh ts, prompt again
    False   present and visible       -> already pending, suppress

``get`` alone collapses the first two into "nothing there", and that
collapse is exactly the bug.

All times are unix-seconds (float), matching the diary tables.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "pending_prompts"

#: Every write from this host is attributed to one node. The flag is
#: written by the listen daemon that observed the denial and cleared by
#: the same host's grant/block path, so SINGLE_WRITER is honest rather
#: than convenient.
_ACTOR = "scitex-agent-container"

__all__ = [
    "STORE_NAME",
    "clear_pending_prompt",
    "has_pending_prompt",
    "init_pending_prompts_schema",
    "open_pending_prompt_store",
    "pending_prompt_store_target",
    "record_pending_prompt",
]


def _schema() -> Any:
    """The pending-prompt schema.

    Built lazily so importing this module does not import scitex-dev; the
    old module was equally lazy about ``state_db``, for the same reason.

    ``(sender, target)`` is the composite IDENTITY, unchanged from the
    original PRIMARY KEY. Identity fields must be IMMUTABLE and the
    store enforces it: "changing one does not update the record, it names
    a different record", which is exactly right for a pair.

    ``ts`` is LAST_WRITER_WINS rather than IMMUTABLE, and that is load
    bearing: a pair that was cleared and then re-denied must carry the
    time of the NEW prompt, not the old one. Nothing orders decisions on
    this field today, so refreshing it cannot reorder anything.
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

    return Schema(
        name=STORE_NAME,
        fields={
            "sender": ident(FieldKind.TEXT),
            "target": ident(FieldKind.TEXT),
            "ts": FieldPolicy(
                kind=FieldKind.REAL,
                role=FieldRole.DATA,
                required=True,
                merge=MergeRule.LAST_WRITER_WINS,
                indexed=False,
            ),
        },
    )


def pending_prompt_store_target() -> Any:
    """Resolve WHERE the pending-prompt flags live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_pending_prompt_store() -> Store:
    """Open the flag store. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function opens and closes one
    per call, mirroring the old ``with open_db(...)`` shape: this runs on a
    denial and on a decision, not on a request path.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        pending_prompt_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_pending_prompts_schema() -> str:
    """Create the flag tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string. It names WHERE the
    state actually went, so an operator can check it rather than assume it.
    """
    store = open_pending_prompt_store()
    try:
        return str(pending_prompt_store_target().locator)
    finally:
        store.close()


def record_pending_prompt(*, sender: str, target: str) -> bool:
    """Mark ``(sender, target)`` as "prompt-emitted, awaiting decision".

    Returns ``True`` iff this call is the FIRST pending-prompt for the pair
    (caller should emit the receiver-facing push). Returns ``False`` when a
    pending flag already exists (caller suppresses re-prompt).

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError``.

    THE THREE-WAY BRANCH IS THE POINT — see the module docstring. A pair
    that was CLEARED is hidden, not absent, and must be re-armed rather
    than treated as already-pending; collapsing those two states is how
    this becomes a prompt that never fires again.

    CONCURRENCY IS PRESERVED, and the requirement is unchanged: the check +
    insert must behave as one atomic step so a concurrent burst of denied
    attempts emits exactly one prompt. Here the insert carries
    ``expected_revision=NEW_RECORD``, so a racing writer that created the
    record between our check and our write raises ``RevisionMismatchError``
    — which means "someone else prompted". Nothing else is caught: an
    unreachable store must
    still be loud.
    """
    if not sender or not target:
        raise ValueError("record_pending_prompt: sender and target must be non-empty")

    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    key = {"sender": sender, "target": target}
    store = open_pending_prompt_store()
    try:
        hidden = store.is_hidden(key)
        if hidden is False:
            return False
        if hidden is True:
            store.unhide(key, expected_revision=ANY_REVISION)
            store.put({**key, "ts": time.time()}, expected_revision=ANY_REVISION)
            return True
        try:
            store.put({**key, "ts": time.time()}, expected_revision=NEW_RECORD)
        except RevisionMismatchError:
            return False
        return True
    finally:
        store.close()


def has_pending_prompt(*, sender: str, target: str) -> bool:
    """Return True iff ``(sender, target)`` has a pending prompt awaiting
    the receiver's block/unblock decision.

    ``get`` excludes hidden records by default, so a cleared pair reads as
    absent here — precisely the DELETE semantics this replaces.
    """
    if not sender or not target:
        return False
    store = open_pending_prompt_store()
    try:
        return store.get({"sender": sender, "target": target}) is not None
    finally:
        store.close()


def clear_pending_prompt(*, sender: str, target: str) -> bool:
    """Clear the pending flag for ``(sender, target)``. Returns True iff a
    visible flag was cleared. Idempotent on absent or already-cleared pairs.

    Called from both decision paths: ``grant_send`` / ``block_send``
    (unblock or block) clear the pending prompt in the same workflow.

    HIDE RATHER THAN DELETE. The store has no delete verb, and that is the
    better fit: the decision stays in the oplog with the actor that made
    it, so "who cleared this, and when" survives — which a DELETE destroyed.
    The read side is unchanged because ``get`` skips hidden records.
    """
    if not sender or not target:
        return False

    from scitex_dev.store import ANY_REVISION

    key = {"sender": sender, "target": target}
    store = open_pending_prompt_store()
    try:
        if store.get(key) is None:
            return False
        store.hide(key, expected_revision=ANY_REVISION)
        return True
    finally:
        store.close()
