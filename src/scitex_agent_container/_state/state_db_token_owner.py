"""WHO holds this Telegram bot right now — answerable without asking Telegram.

THE QUESTION THIS EXISTS TO ANSWER
==================================
Telegram admits ONE ``getUpdates`` consumer per bot token, globally. When two
agents take the same bot, the two ways to find out are both bad: ask Telegram
(which answers 409, to both, forever, and names neither) or scan ``/proc`` on
every host (which :mod:`..runtimes._cct_poller_singleton` does, and which is
host-scoped and blind to another uid's ``environ``).

So every agent that resolves a bot token RECORDS the claim as it starts. After
that, "who owns ``sha256:00ec09b9ad73``?" is a query.

WRITE-ONLY, ON PURPOSE, FOR NOW
===============================
Nothing reads this to make a decision. No start is refused, delayed or altered
by what is in here. Enforcement — refusing to start an agent whose token is
already held by a LIVE owner — is a separate change with a large blast radius,
and building it on a computation that has never been observed in production is
how a refusal ships with a bug in it. This lays the record down first so that
the refusal, when it comes, can be shown to have been right.

THE IDENTITY IS (token_fp, host, agent), AND THAT IS THE POINT
==============================================================
The obvious key is ``token_fp`` alone — "one row per bot, holding its current
owner". That key DESTROYS the evidence: the second claimant's write would
overwrite the first's, and a store whose whole purpose is to reveal double
ownership would render every collision as a single tidy row. Measured on the
live fleet 2026-08-22, three bot tokens are claimed by two specs each, two of
them ACROSS HOSTS — exactly the rows a ``token_fp`` key would have collapsed.

With the triple key, a contended token simply has more than one row, and the
contention is visible by counting. :func:`owners_of` is that count.

THE DATA FIELDS ARE LAST_WRITER_WINS, unlike ``verdict_delivered``'s IMMUTABLE
ones, and for the opposite reason: a delivered verdict is a historical fact,
whereas this row is CURRENT STATE. The same agent restarting on the same host
with a new pid must move ``pid`` and ``started_at``, not create a second row
and not be refused.

FINGERPRINTS ONLY. ``token_fp`` is the opaque ``sha256:<12hex>`` from
:func:`.._account._rotation_audit.fingerprint_token`. No token value is passed
to, stored by, or retrievable from anything here.

PostgreSQL-only, by the operator's 2026-08-19 order ("fail fast, fail loud, no
fallbacks") and by adopting :mod:`scitex_dev.store` exactly as
:mod:`.state_db_verdict_dedup` and :mod:`.state_db_incarnations` do. An
unreachable store RAISES; the START-path caller
(:mod:`..runtimes._cct_token_ledger`) is what turns that into a printed warning
rather than a failed boot, because a ledger that can break a start is worse
than a missing ledger.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "cct_token_owner"

#: Every write from this host is attributed to one node. Each host records the
#: claims made ON it, so SINGLE_WRITER is the honest policy: no host ever
#: rewrites another host's row.
_ACTOR = "scitex-agent-container"


def _schema() -> Any:
    """The token-ownership schema.

    Built lazily so importing this module does not import scitex-dev (import
    cost off the hot path — the same reason its siblings are lazy).

    IDENTITY fields must be IMMUTABLE; the store enforces it, and the reason
    is worth keeping: "changing one does not update the record, it names a
    different record." That is exactly right here — a different agent holding
    the same bot IS a different record, and preserving it is the whole point.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any, *, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=indexed,
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
            # --- who holds what, where: the identity triple ---
            "token_fp": ident(FieldKind.TEXT, indexed=True),
            "host": ident(FieldKind.TEXT),
            "agent": ident(FieldKind.TEXT),
            # --- facts about the current holding, refreshed on restart ---
            "pid": fact(FieldKind.INTEGER),
            "started_at": fact(FieldKind.REAL, required=True),
            "source": fact(FieldKind.TEXT),
            "slot": fact(FieldKind.TEXT),
        },
    )


def token_owner_store_target() -> Any:
    """Resolve WHERE the ownership ledger lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_token_owner_store() -> Store:
    """Open the ownership ledger. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function here opens and closes
    one per call, mirroring its siblings: a claim is recorded once per agent
    start, so the connection cost sits on a launch path, never a request path.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        token_owner_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_token_owner_schema() -> str:
    """Create the ownership tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string, which names WHERE the
    state actually went so an operator can check rather than assume.
    """
    store = open_token_owner_store()
    try:
        return str(token_owner_store_target().locator)
    finally:
        store.close()


def record_token_owner(
    *,
    token_fp: str,
    agent: str,
    host: str,
    pid: int | None = None,
    started_at: float | None = None,
    source: str = "",
    slot: str = "",
) -> None:
    """Record that ``agent`` on ``host`` holds the bot ``token_fp``.

    UPSERT, not insert-or-ignore: re-recording the same ``(token_fp, host,
    agent)`` REFRESHES ``pid`` and ``started_at`` in place, because this row
    is current state and a restart is the normal case. It never creates a
    second row for the same holder, and it never touches another holder's row
    — which is what leaves a contended token visibly holding two.

    ``expected_revision=ANY_REVISION`` because this is an upsert on purpose:
    unlike ``verdict_delivered``, which uses ``NEW_RECORD`` to make a re-seen
    row a no-op, a re-seen row HERE is a restart and must land. There is no
    lost-update hazard to guard against — a claim is written by the process
    that owns it, so the only writer that races us is the same agent starting
    again, and its values are the ones we would want anyway.

    ``token_fp`` must already be a fingerprint. Passing a raw token here would
    store a secret; the only supported producer is
    :func:`.._account._rotation_audit.fingerprint_token`, and a value that does
    not look like its output is refused rather than written.
    """
    from scitex_dev.store import ANY_REVISION

    if not token_fp or not str(token_fp).startswith("sha256:"):
        raise ValueError(
            "record_token_owner takes a FINGERPRINT, not a token: expected a "
            "'sha256:<12hex>' string from fingerprint_token(), got "
            f"{'an empty value' if not token_fp else 'something else'}. "
            "Refusing to write, because the one thing this ledger must never "
            "hold is a token value."
        )
    key = {"token_fp": token_fp, "host": host, "agent": agent}
    ts = float(started_at) if started_at is not None else time.time()
    store = open_token_owner_store()
    try:
        store.put(
            {
                **key,
                "pid": int(pid) if pid is not None else None,
                "started_at": ts,
                "source": source,
                "slot": slot,
            },
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()


def owners_of(token_fp: str) -> list[dict]:
    """Every recorded holder of ``token_fp``, newest claim first.

    More than one row means more than one agent has claimed this bot. That is
    not by itself a live conflict — a row survives its process — so this
    reports and does not judge; the judging is
    :mod:`..runtimes._cct_token_collision` (static) and
    :mod:`..runtimes._cct_poller_singleton` (live).
    """
    return [r for r in token_owner_rows() if r.get("token_fp") == token_fp]


def token_owner_rows() -> list[dict]:
    """The whole ledger, newest claim first. Returns [] on a brand-new store."""
    store = open_token_owner_store()
    try:
        rows: list[Row] = store.rows()
    finally:
        store.close()
    return sorted(
        (dict(r.values) for r in rows),
        key=lambda v: float(v.get("started_at") or 0.0),
        reverse=True,
    )


__all__ = [
    "STORE_NAME",
    "init_token_owner_schema",
    "open_token_owner_store",
    "owners_of",
    "record_token_owner",
    "token_owner_rows",
    "token_owner_store_target",
]
