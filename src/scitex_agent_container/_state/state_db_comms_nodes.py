"""``comms_nodes`` — the A2A peer registry, on PostgreSQL (2026-08-28).

The cross-host ``name -> (host, a2a_port)`` directory that resolves A2A
targets (ADR-0014). Public symbols are re-exported from
:mod:`state_db_nodes`, so callers keep the natural import path
``from ..._state.state_db_nodes import register_comms_node``.

WHY THIS MOVED OFF SQLite
=========================
Operator ruling (2026-08-28): 「スクライトなんて全部絶滅させてください」.
For THIS table the ruling is not a preference, it is the bug report. A
per-host ``state.db`` means a different peer directory per host, and the
whole of ADR-0014 — ``source_host``, the anti-entropy ``sac registry
sync``, a tombstone that has to survive an ``INSERT OR IGNORE`` — is
machinery built to paper over that. One shared store deletes the problem
the machinery was written for: a row registered anywhere is visible
everywhere, immediately, with no pull.

The move ADOPTS :mod:`scitex_dev.store`, as :mod:`.state_db_diary` and
:mod:`.state_db_grants` did. Its ``host_store`` resolves in exactly two
steps (``SCITEX_STORE_DSN`` or the per-host PostgreSQL) with NO SQLite
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach.

``db_path`` IS GONE from all five signatures. It named a SQLite file;
there is no file. Test isolation now comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema (the ``pg_schema`` fixture),
which is better isolation than a temp path was because it exercises the
real resolver.

THREE PROPERTIES THIS MIGRATION HAD TO PRESERVE
===============================================
1. THE TOMBSTONE IS ``Store.hide``, NOT A DELETE — and not a column.
   ``unregister_comms_node`` set ``ended_at`` and kept the row precisely
   so "this name was never here" and "this name left" stayed
   distinguishable; for a routing registry that difference is the whole
   answer to "why is nothing reaching agent X?". ``hide()`` is the
   store's only removal and says exactly that: the record, its values
   and its history stay readable through ``include_hidden=True`` and in
   the oplog, while every default read treats it as absent.
   :func:`lookup_comms_node` therefore still returns ``None`` right
   after an unregister — the routing behaviour is unchanged.

   ``ended_at`` survives as an AUDIT STAMP, not as the liveness flag.
   ``hidden`` is the single source of truth for "is this live"; the
   field records only WHEN the tombstone was written, which a bool
   cannot carry. Both are written together and cleared together on
   re-registration, so they cannot drift.

2. THE CONFLICT POLICY IS UNCHANGED, BRANCH FOR BRANCH AND IN ORDER.
   ADR-0014 chose fail-loud (α) over last-writer-wins (β), operator
   directive 12847 hardened it, and a 2026-08-20 incident added the
   tombstone exemption. The cross-host check still runs BEFORE that
   exemption, because a tombstone does not answer WHO OWNS THE NAME.

3. THE LISTING ORDER STAYS ALPHABETICAL BY ``name``, AND THAT IS NOT AN
   OVERSIGHT. Its sibling :mod:`.state_db_grants` had to replace
   ``ORDER BY rowid`` with the HLC, because ``rowid`` meant INSERTION
   ORDER and its only wall-clock substitute (``created_at``) ties on
   bulk-imported peer rows and skews across hosts — which is how a
   leaked grant once hid in a listing. THIS module never ordered by
   ``rowid`` or by any clock: the documented contract is "by ``name``
   ascending", and ``name`` is the IDENTITY — total, stable, tie-free,
   immune to clock skew. It needs no successor, so it keeps none.

``source_host`` IS KEPT, against the sketch in :mod:`.._store_plugin`
which proposed replacing it with the oplog's ``_origin``. They are not
the same fact: ``_origin`` is PROVENANCE (which node accepted the op),
while ``source_host`` is a CALLER-DECLARED claim (``None`` locally, the
peer's name when relayed) and it is the discriminator the cross-host
branch keys off. Dropping it would fold the "two hosts claim one name"
conflict — which ALWAYS raises — into the same-source one, which
``replace=True`` can override: a silent loosening of a registry's
uniqueness rule.
"""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "COMMS_NODES_STORE",
    "CommsNodeConflictError",
    "RegisterCommsNodeKind",
    "list_comms_nodes",
    "lookup_comms_node",
    "register_comms_node",
    "resolve_comms_node_host",
    "unregister_comms_node",
    "writer_policy",
]

#: Logical store name. Renders as four physical tables
#: (``comms_nodes_rows``, ``_oplog``, ``_identity``, ``_cursor``).
COMMS_NODES_STORE = "comms_nodes"

_ACTOR = "scitex-agent-container"


RegisterCommsNodeKind = Literal["spec", "self-peer", "manual"]
"""Discriminator passed by callers of :func:`register_comms_node`.

``spec`` — the canonical container-spec path (``_lifecycle/_instances``
after a spec-driven ``sac start``). ``self-peer`` — a self-peer
registration path (``_mcp/_channel_self_register`` or
``_listen/_self_peer_persistence``). ``manual`` — operator-driven
``sac registry register``, or a test; the default, so callers that
don't pass one keep working.

NOT PERSISTED — it flows into :class:`CommsNodeConflictError`'s message
so the operator sees WHICH path tried to overwrite WHICH.
"""


class CommsNodeConflictError(RuntimeError):
    """Two registrations disagree on a ``name``'s ``(host, a2a_port)``.

    Raised by :func:`register_comms_node` whenever a write would
    silently OVERWRITE an existing record with a different
    ``(host, a2a_port)``. Two collision shapes share this exception
    (operator directive 12847 — fail-loud, no silent winner):

    1. **Cross-host conflict.** The existing record was relayed from
       ``source_host=A``; the caller registers with ``source_host=B``
       and a different ``(host, a2a_port)``. Two hosts independently
       claim the same name — neither has authority over the other.
    2. **Same-source different-target conflict (PR L1).** Both sides
       carry the SAME ``source_host`` (e.g. both are local
       registrations with ``source_host=None``) but the caller's
       ``(host, a2a_port)`` differs from what is stored. This silently
       last-writer-won until PR L1; that is the exact silent-shadow the
       operator's directive locks out. The caller must pass
       ``replace=True`` to opt into the overwrite.
    """


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=True,
    )


def _data(kind: Any, merge: Any, *, required: bool = False) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=merge,
        indexed=False,
    )


def _comms_nodes_schema() -> Any:
    """The schema. Every merge rule below is a claim about the domain.

    ``registered_at`` is IMMUTABLE because it is a HISTORICAL FACT: when
    this name first entered the directory. Two hosts claiming one name
    with different registration times is precisely the collision
    :class:`CommsNodeConflictError` exists for, and IMMUTABLE reports it
    as a MergeConflict (kept / rejected / reason) rather than quietly
    picking one.

    ``host`` / ``a2a_port`` are LAST_WRITER_WINS because a placement
    genuinely moves — ``spec.a2a.port: auto`` makes "a different port"
    the NORMAL outcome of a restart.

    ``updated_at`` is MAX, not LAST_WRITER_WINS: it is the record's own
    clock and a late-arriving stale replica must not walk it backwards.
    (The rule ``node_comms_policy.updated_at`` already carries in
    :mod:`.._store_plugin`.)
    """
    from scitex_dev.store import FieldKind, MergeRule, Schema

    return Schema(
        name=COMMS_NODES_STORE,
        fields={
            "name": _ident(FieldKind.TEXT),
            "host": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS, required=True),
            "a2a_port": _data(
                FieldKind.INTEGER, MergeRule.LAST_WRITER_WINS, required=True
            ),
            "registered_at": _data(FieldKind.REAL, MergeRule.IMMUTABLE, required=True),
            "updated_at": _data(FieldKind.REAL, MergeRule.MAX, required=True),
            "source_host": _data(FieldKind.TEXT, MergeRule.LAST_WRITER_WINS),
            # The audit stamp beside the hide flag — see the module
            # docstring. NOT the liveness test; ``hidden`` is.
            "ended_at": _data(FieldKind.REAL, MergeRule.LAST_WRITER_WINS),
        },
    )


def writer_policy() -> Any:
    """MULTI_WRITER, and not as a preference.

    SINGLE_WRITER would make existing, correct call paths illegal
    writes. A record here has no single stable owner, MEASURED from the
    callers:

    * ``cli_pkg/lifecycle/_dispatch.py`` registers a record from the
      DISPATCHING host describing a placement on a DIFFERENT host
      (``register_comms_node(name=..., host=peer, ...)``).
    * ``cli_pkg/lifecycle/_forget.py`` and ``_stop.py --force``
      tombstone an agent from wherever the operator typed the command,
      routinely not the agent's own host.
    * ``sac registry register`` is an operator repair verb run anywhere.

    Under SINGLE_WRITER the first of those is a ``WriterConflictError``
    on an ordinary cross-host spawn. (``.._store_plugin`` sketches this
    schema as SINGLE_WRITER; that sketch predates a reading of the
    callers and is wrong on this point.)

    A FUNCTION RATHER THAN AN INLINE ARGUMENT so the choice can be read
    back WITHOUT opening a connection. ``Store.__init__`` connects, so a
    test asserting ``store.writer_policy`` would need a live PostgreSQL
    to check a pure declaration — and would SKIP, silently, on every
    host without one.
    """
    from scitex_dev.store import WriterPolicy

    return WriterPolicy.MULTI_WRITER


def _open() -> "Store":
    """Open the comms_nodes store. RAISES if PostgreSQL is unreachable."""
    from scitex_dev.store import Store, host_store

    schema = _comms_nodes_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=writer_policy(),
        actor=_ACTOR,
    )


def _as_dict(row: Any) -> dict[str, Any]:
    """One store row in the historical ``comms_nodes`` dict shape.

    ``row.values`` is the accessor — ``Row`` is a frozen dataclass whose
    ``key`` is a TUPLE of the identity values; it exposes no ``.fields``
    and is not iterable.
    """
    values = dict(row.values)
    source_host = values.get("source_host")
    ended_at = values.get("ended_at")
    return {
        "name": str(values["name"]),
        "host": str(values["host"]),
        "a2a_port": int(values["a2a_port"]),
        "registered_at": float(values["registered_at"]),
        "updated_at": float(values["updated_at"]),
        "source_host": (str(source_host) if source_host is not None else None),
        "ended_at": (float(ended_at) if ended_at is not None else None),
    }


def register_comms_node(
    *,
    name: str,
    host: str,
    a2a_port: int,
    source_host: str | None = None,
    kind: RegisterCommsNodeKind = "manual",
    source_path: str | None = None,
    replace: bool = False,
) -> None:
    """Idempotent upsert into the ``comms_nodes`` store.

    Behaviour, unchanged from the SQLite version:

    * No existing record → INSERT, with ``registered_at`` and
      ``updated_at`` at ``time.time()``.
    * Matching ``(host, a2a_port)`` → bump ``updated_at``, and un-hide
      if it was tombstoned (the natural way a "node came back"
      converges).
    * DIFFERENT ``(host, a2a_port)`` and a different ``source_host`` →
      raise :class:`CommsNodeConflictError`. Two hosts independently
      claim the name; operator-rename is the only resolution.
    * DIFFERENT ``(host, a2a_port)``, the SAME ``source_host``, and
      TOMBSTONED → adopt the new target and un-hide. A dead record is a
      record of a PAST placement, not a live claim, so it must not
      refuse the restart that follows it (measured on two hosts
      2026-08-20). Cross-host conflicts are deliberately NOT covered —
      that check runs first and still raises.
    * DIFFERENT ``(host, a2a_port)``, the SAME ``source_host``, LIVE →
      raise unless ``replace=True``. Default callers do not set it; they
      catch and log, so a real collision surfaces in the operator's logs
      and no record is silently shadowed.

    ``kind`` and ``source_path`` are NOT persisted — they only enrich
    the conflict message. ``replace`` has no effect on the cross-host
    conflict, which ALWAYS raises.
    """
    if not name:
        raise ValueError("register_comms_node: name must be non-empty")
    if not host:
        raise ValueError("register_comms_node: host must be non-empty")
    if not isinstance(a2a_port, int) or isinstance(a2a_port, bool) or a2a_port <= 0:
        raise ValueError(
            f"register_comms_node: a2a_port must be a positive int, got {a2a_port!r}"
        )

    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    now = time.time()
    store = _open()
    try:
        key = {"name": name}
        # include_hidden: a tombstoned record still OCCUPIES the identity,
        # so a default read would say "absent" and the insert below would
        # collide with a record the caller was never shown.
        existing = store.get(key, include_hidden=True)
        if existing is None:
            store.put(
                {
                    "name": name,
                    "host": host,
                    "a2a_port": a2a_port,
                    "registered_at": now,
                    "updated_at": now,
                    "source_host": source_host,
                    "ended_at": None,
                },
                expected_revision=NEW_RECORD,
            )
            return

        values = dict(existing.values)
        same_target = str(values["host"]) == host and int(values["a2a_port"]) == a2a_port
        existing_source = values.get("source_host")

        if same_target:
            # Idempotent — bump updated_at and lift any tombstone.
            store.put(
                {
                    "name": name,
                    "updated_at": now,
                    "ended_at": None,
                    "source_host": source_host,
                },
                expected_revision=ANY_REVISION,
            )
            if existing.hidden:
                store.unhide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
            return

        if existing_source != source_host:
            raise CommsNodeConflictError(
                f"comms_nodes name conflict for {name!r}: "
                f"existing=(host={values['host']!r}, "
                f"port={int(values['a2a_port'])}, "
                f"source={existing_source!r}) "
                f"new=(kind={kind!r}, host={host!r}, port={a2a_port}, "
                f"source={source_host!r}, source_path={source_path!r}). "
                f"ADR-0014: names are globally unique. Rename or "
                f"unregister one of them."
            )

        if existing.hidden:
            store.put(
                {
                    "name": name,
                    "host": host,
                    "a2a_port": a2a_port,
                    "updated_at": now,
                    "ended_at": None,
                    "source_host": source_host,
                },
                expected_revision=ANY_REVISION,
            )
            store.unhide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
            return

        if not replace:
            other_kind = "spec" if kind == "self-peer" else "self-peer pointer"
            raise CommsNodeConflictError(
                f"comms_nodes silent-overwrite refused for {name!r} "
                f"(operator directive 12847, PR L1): "
                f"existing=(host={values['host']!r}, "
                f"port={int(values['a2a_port'])}, "
                f"source={existing_source!r}) "
                f"incoming=(kind={kind!r}, host={host!r}, "
                f"port={a2a_port}, source={source_host!r}, "
                f"source_path={source_path!r}). "
                f"Two registrations for the same name disagree on the "
                f"(host, a2a_port) target. Resolve by either:\n"
                f"  - rerunning the canonical writer with "
                f"`--prefer {kind}` to declare intent (overwrites), or\n"
                f"  - removing/renaming the conflicting {other_kind} "
                f"so a single source owns this name."
            )

        # Explicit replace — operator-confirmed via the --prefer flag.
        # ``source_host`` is deliberately NOT rewritten: this branch is
        # same-source by construction, and the SQLite UPDATE it replaces
        # did not touch the column either.
        store.put(
            {
                "name": name,
                "host": host,
                "a2a_port": a2a_port,
                "updated_at": now,
                "ended_at": None,
            },
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()


def unregister_comms_node(*, name: str) -> bool:
    """Tombstone the record. ``True`` iff a LIVE record was tombstoned.

    Hides rather than deletes — the exact successor of the SQLite
    ``ended_at`` soft tombstone: the record and its history stay
    readable through ``include_hidden=True`` and in the oplog, while
    :func:`lookup_comms_node` and :func:`resolve_comms_node_host` see it
    as absent, so a tombstoned name stops resolving immediately.
    ``ended_at`` is stamped alongside the hide so the audit trail keeps
    the WHEN a boolean cannot carry. Re-running on an already-tombstoned
    record is a no-op returning ``False``.
    """
    if not name:
        return False

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        key = {"name": name}
        if store.get(key) is None:
            # Absent, or already hidden — either way nothing was live,
            # which is what the SQLite ``rowcount == 0`` meant.
            return False
        now = time.time()
        store.put(
            {"name": name, "ended_at": now, "updated_at": now},
            expected_revision=ANY_REVISION,
        )
        store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        return True
    finally:
        store.close()


def lookup_comms_node(*, name: str) -> dict[str, Any] | None:
    """Return the LIVE ``comms_nodes`` record for ``name``, or ``None``.

    Tombstoned (hidden) records read as absent — for the resolver they
    are equivalent to "not present". Callers that need to *see*
    tombstones use :func:`list_comms_nodes` with ``include_ended=True``.
    """
    if not name:
        return None

    store = _open()
    try:
        row = store.get({"name": name})
    finally:
        store.close()
    return None if row is None else _as_dict(row)


def resolve_comms_node_host(*, name: str) -> dict[str, Any] | None:
    """Resolver-shaped lookup for cross-host A2A forwarding.

    Returns ``{host, a2a_port}`` (matching the
    :func:`state_db_nodes.resolve_node_host` shape) or ``None`` when the
    name is missing OR tombstoned. Used by ``resolve_node_host`` as the
    fallback after the ``instances`` lookup misses.
    """
    info = lookup_comms_node(name=name)
    if info is None:
        return None
    return {"host": info["host"], "a2a_port": info["a2a_port"]}


def list_comms_nodes(*, include_ended: bool = False) -> list[dict[str, Any]]:
    """Return every ``comms_nodes`` record as a list of dicts.

    Default omits tombstoned records; pass ``include_ended=True`` for the
    full set. Ordered by ``name`` ascending — the documented contract,
    kept deliberately rather than migrated to the HLC. See the module
    docstring: ``name`` is the IDENTITY, so the order is already total,
    stable, tie-free and immune to the clock skew that forced
    :mod:`.state_db_grants` off its wall-clock sort.
    """
    store = _open()
    try:
        # rows() excludes hidden by default, which IS the tombstone
        # filter — spelled out because the exclusion is load-bearing.
        rows = store.rows(include_hidden=include_ended)
    finally:
        store.close()
    return sorted((_as_dict(row) for row in rows), key=lambda r: r["name"])
