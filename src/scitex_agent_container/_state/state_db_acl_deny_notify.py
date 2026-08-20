"""ACL-deny rate-limited synthetic-notification log (sac-comms item D).

Per lead a2a ``c42b3e3c`` (2026-06-13) — merged with
``lead-sac-acl-blocked-attempt-notification``:

  * When an outbound ``a2a_send(sender, target)`` is ACL-denied (403),
    the TARGET (receiver) gets a *synthetic* system-level inbox
    notification: "Sender X attempted a send to you and was blocked
    by ACL; grant via `sac a2a grant X <you>` if intended."
  * The notification *bypasses* ACL — it is published directly onto
    the receiver's inbox channel so the operator can grant
    proactively instead of reactively.
  * Rate-limited per ``(sender, target)`` pair: at most one
    notification per cool-down window (default 30 min, env-overridable
    via ``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S``).
  * This REPLACES the prior parent/child auto-grant policy. The
    rate-limited notification is the substitute for "auto-grant if
    intra-lineage": the operator sees the attempt once per cooldown
    window and decides.

This module owns the rate-limit log only — the actual notification
mint + publish lives in ``_listen._node_channel`` (which calls
:func:`should_notify_acl_deny` to decide whether to push or
suppress the redundant frame).

Schema is minimal: ``(sender, target)`` as the composite identity plus
``last_notified_at``.

WHY THIS MODULE NO LONGER TOUCHES SQLite
========================================
The operator's 2026-08-19 order was to eradicate SQLite and move to
PostgreSQL: "fail fast, fail loud, no fallbacks". This is the fifth
table to move, and it lands in the same PR as ``comms_blocks`` because
the two are siblings from the same task, both empty, and both remove a
line from the same block of ``state_db.init_schema`` — splitting them
would only manufacture a third merge conflict in that function.

``db_path`` IS GONE from every function. It named a SQLite file; there
is no file.

THE ATOMIC CHECK-AND-UPDATE IS PRESERVED, BY A DIFFERENT MECHANISM
==================================================================
The paragraph this replaced promised that "the check-and-update is
atomic within a single transaction so a concurrent burst of denied
attempts publishes at most one notification per cooldown window". That
promise is the module's whole point — a rate-limit log that can be
raced is not a rate limit — and the store has no transactions, so it
would have been the easy thing to lose.

It is kept with the store's OPTIMISTIC concurrency instead. The read
returns the record's REVISION; the write passes that same revision as
``expected_revision``. A racing writer that bumped it first makes ours
raise ``RevisionMismatchError``, which means "someone else just
notified this pair" — precisely the answer the transaction gave, and
the caller is told to suppress. A first notification uses
``NEW_RECORD`` for the same reason.

This is the first table in the migration where the EXACT revision is
load-bearing. The earlier slices could use ``ANY_REVISION`` because
losing a race there cost nothing; here it costs a duplicate frame in a
receiver's inbox, which is the thing being prevented.

No-mocks (PA-306) testing — a real PostgreSQL via the shared
``pg_schema`` fixture, no monkeypatch.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "acl_deny_notify_log"

#: Every write from this host is attributed to one node. The log is written by
#: the listen daemon that observed the denial, so SINGLE_WRITER is honest.
_ACTOR = "scitex-agent-container"

__all__ = [
    "DEFAULT_COOLDOWN_S",
    "STORE_NAME",
    "deny_notify_store_target",
    "ensure_acl_deny_notify_log_table",
    "last_notified_at",
    "open_deny_notify_store",
    "resolve_cooldown_s",
    "should_notify_acl_deny",
]


# 30 minutes — sensible default per lead's directive. Long enough to
# avoid receiver-side flood from a misbehaving sender; short enough
# that an operator who missed the first frame still sees a follow-up
# within their attention window.
DEFAULT_COOLDOWN_S: float = 30.0 * 60.0

# Env knob name (one of the audit-compliant top-level
# ``SCITEX_AGENT_CONTAINER_*``-shape vars would be more idiomatic
# but the operator-facing knob convention for runtime tuning is
# ``SCITEX_*`` — and this is a per-process runtime knob, not a
# packaging-time toggle).
_COOLDOWN_ENV = "SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"


def _schema() -> Any:
    """The deny-notify rate-limit schema.

    Built lazily so importing this module does not import scitex-dev.

    ``(sender, target)`` is the composite IDENTITY — the SQLite table's PRIMARY
    KEY, unchanged. ``last_notified_at`` is LAST_WRITER_WINS: every successful
    notification moves it forward, and that IS the rate limit.
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
            "last_notified_at": FieldPolicy(
                kind=FieldKind.REAL,
                role=FieldRole.DATA,
                required=True,
                merge=MergeRule.LAST_WRITER_WINS,
                indexed=False,
            ),
        },
    )


def deny_notify_store_target() -> Any:
    """Resolve WHERE the rate-limit log lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_deny_notify_store() -> Store:
    """Open the rate-limit log. RAISES if PostgreSQL is unreachable."""
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        deny_notify_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def ensure_acl_deny_notify_log_table() -> str:
    """Create the rate-limit tables if missing. Idempotent.

    Kept under its original name because the name still says what it does.
    Returns the resolved store LOCATOR as a string, so an operator can check
    where the state went rather than assume it.
    """
    store = open_deny_notify_store()
    try:
        return str(deny_notify_store_target().locator)
    finally:
        store.close()


def resolve_cooldown_s(override: float | None = None) -> float:
    """Return the effective cool-down window in seconds.

    Resolution order:

      1. Explicit ``override`` argument (test seam — pass a tiny
         value so a unit test can exercise the elapse case without a
         30-minute wait).
      2. Env var ``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S`` (operator
         runtime knob — accepts a float number of seconds).
      3. :data:`DEFAULT_COOLDOWN_S` (30 minutes).

    A negative / non-numeric env value falls back to the default
    (fail-loud at the parse boundary — silently honouring "-1" would
    disable the rate-limit and surprise the operator).
    """
    if override is not None:
        return float(override)
    raw = os.environ.get(_COOLDOWN_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_COOLDOWN_S
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_COOLDOWN_S
    if parsed < 0:
        return DEFAULT_COOLDOWN_S
    return parsed


def should_notify_acl_deny(
    *,
    sender: str,
    target: str,
    cooldown_s: float | None = None,
    now: float | None = None,
) -> bool:
    """Atomically decide whether to publish a deny-notification frame.

    Returns ``True`` iff the caller should emit a synthetic
    notification to ``target``. On ``True`` the row is upserted
    with the supplied ``now`` timestamp, so a concurrent burst of
    denied attempts publishes at most one notification per cool-down
    window.

    Returns ``False`` when a prior notification was emitted within
    the cool-down window (suppress to avoid receiver flood).

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError`` —
    a deny with no identity has no per-pair key to throttle on.

    ``cooldown_s`` overrides the env / default (test seam).
    ``now`` is the wall clock to record (test seam); defaults to
    :func:`time.time`.
    """
    if not sender or not target:
        raise ValueError("should_notify_acl_deny: sender and target must be non-empty")
    effective_cooldown = resolve_cooldown_s(cooldown_s)
    effective_now = time.time() if now is None else float(now)

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    key = {"sender": sender, "target": target}
    store = open_deny_notify_store()
    try:
        existing = store.get(key)
        if existing is not None:
            elapsed = effective_now - float(existing.values["last_notified_at"])
            if elapsed < effective_cooldown:
                return False
        # First time, or cool-down elapsed — write + return True.
        #
        # THE REVISION IS THE TRANSACTION. The SQLite version held the read and
        # the upsert in one transaction so a burst of denied attempts published
        # ONE notification. Here the read's own revision is passed back as
        # ``expected_revision``: a racing writer that moved it first makes this
        # write raise, and that raise means "someone else just notified this
        # pair" — the same answer, reached optimistically. Passing
        # ANY_REVISION here would compile, pass every single-threaded test, and
        # silently delete the guarantee.
        # ``Row.seq`` IS the revision, verified by experiment rather than by
        # its name: writing with a STALE seq raises RevisionMismatchError,
        # which is the only thing that makes it usable as an optimistic lock.
        # (``Row`` has no ``.revision`` attribute — the first version of this
        # line assumed one and died with AttributeError on the first test that
        # reached the update path.)
        expected = NEW_RECORD if existing is None else existing.seq
        try:
            store.put({**key, "last_notified_at": effective_now}, expected_revision=expected)
        except RevisionMismatchError:
            return False
    finally:
        store.close()
    return True


def last_notified_at(
    *,
    sender: str,
    target: str,
) -> float | None:
    """Return the timestamp of the most recent deny-notify for the pair.

    Returns ``None`` when no notification has ever been published for
    the pair. Observability / test surface — the rate-limit decision
    itself goes through :func:`should_notify_acl_deny`, which keeps the
    check+write atomic against a concurrent burst.
    """
    if not sender or not target:
        return None
    store = open_deny_notify_store()
    try:
        record = store.get({"sender": sender, "target": target})
    finally:
        store.close()
    if record is None:
        return None
    return float(record.values["last_notified_at"])
