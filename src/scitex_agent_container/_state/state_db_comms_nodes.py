"""``comms_nodes`` primitives — the symmetric federated comms graph (ADR-0014).

The cross-host directory: "agent ``<name>`` is reachable at
``host:a2a_port``". :func:`..state_db_nodes.resolve_node_host` consults it
when no live ``instances`` row matches, which is what lets a Spartan agent
address ``lead`` at all.

Public symbols are re-exported from :mod:`state_db_nodes` so callers keep
using ``from ..._state.state_db_nodes import register_comms_node``.

ON POSTGRESQL SINCE 2026-08-28. The schema, the ``Store`` factory and the
row codec live in :mod:`.state_db_comms_nodes_store`, whose docstring
carries the whole storage argument: why the sync layer is gone, and which
three SQLite columns did not move because the primitive already owns the
concepts they hand-rolled (``ended_at`` → ``hide()``, ``source_host`` →
``_origin``, ``updated_at`` → the HLC). This file holds the VERBS, and their
policy is unchanged — the storage moved, ADR-0014's fail-loud conflict rule
did not.

``db_path`` IS GONE from every signature below. It named a SQLite file;
there is no file.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal

from .state_db_comms_nodes_store import (
    ACTOR,
    COMMS_NODES_STORE,
    comms_node_as_dict,
    comms_nodes_schema,
    new_comms_nodes_store,
    open_comms_nodes_store,
    reset_comms_nodes_store,
    run_with_reconnect,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row

__all__ = [
    "COMMS_NODES_STORE",
    "CommsNodeConflictError",
    "RegisterCommsNodeKind",
    "comms_nodes_schema",
    "list_comms_nodes",
    "lookup_comms_node",
    "new_comms_nodes_store",
    "open_comms_nodes_store",
    "register_comms_node",
    "rename_comms_node",
    "reset_comms_nodes_store",
    "run_with_reconnect",
    "resolve_comms_node_host",
    "unregister_comms_node",
]


RegisterCommsNodeKind = Literal["spec", "self-peer", "manual"]
"""Discriminator passed by callers of :func:`register_comms_node`.

``spec`` — the record is being written from the canonical container-spec
path (``_lifecycle/_instances._record_local_instance`` after a spec-driven
``sac start``).

``self-peer`` — the record is being written from a self-peer registration
path (``_mcp/_channel_self_register.register_self_node`` or the Q4
``_listen/_self_peer_persistence.persist_discovered_self_peers``).

``manual`` — operator-driven ``sac registry register`` or a test fixture.
Default when the caller doesn't pass one explicitly. The kind is NOT
persisted (it is not a field of the declared schema) — it flows into
:class:`CommsNodeConflictError`'s message so the operator sees WHICH path
tried to overwrite WHICH.
"""


class CommsNodeConflictError(RuntimeError):
    """Two registrations disagree on a ``name``'s ``(host, a2a_port)``.

    Raised by :func:`register_comms_node` whenever a write would silently
    OVERWRITE an existing record with a different ``(host, a2a_port)``. Two
    collision shapes share this exception (operator directive 12847 —
    fail-loud, no silent winner):

    1. **Cross-host conflict.** The stored record was written by node A
       (its ``_origin``); the caller is writing from node B with a different
       ``(host, a2a_port)``. Two hosts independently claim the same name —
       neither has authority over the other.
    2. **Same-source different-target conflict (PR L1).** The stored record
       and the caller share an origin (both are this host's own
       registrations) but the caller's ``(host, a2a_port)`` differs from what
       is stored. Before PR L1 this silently last-writer-wins; that is the
       exact silent-shadow the operator's directive locks out. The caller
       must pass ``replace=True`` to opt into the overwrite (only reached
       deliberately through the ``--prefer`` flag, which is the
       explicit-client-option half of the directive).

    ADR-0014 conflict policy: fail-loud (α) over last-writer-wins (β). The
    exception carries enough context (kind, source_path, existing host/port +
    origin, new host/port + source) for the caller's log line to point at the
    misconfig directly.

    The discriminator CHANGED SOURCE in the PostgreSQL move and the behaviour
    did not: shape 1 used to compare a hand-written ``source_host`` column,
    which was NULL for every locally registered row. It now compares the
    record's ``_origin``, which the primitive stamps on every op — so the
    check that decides "is this a different host claiming my name" no longer
    depends on a writer having remembered to fill a column.
    """


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


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
    """Idempotent upsert of ``name`` → ``(host, a2a_port)``.

    Behaviour (unchanged from the SQLite version — the storage moved, the
    policy did not):

    * No existing record → insert one, stamping ``registered_at``.
    * Existing record with matching ``(host, a2a_port)`` → a no-op write
      that restamps the clock, and un-hides a withdrawn record. That is the
      natural way a "node came back" converges.
    * Existing record with a DIFFERENT ``(host, a2a_port)`` written by a
      DIFFERENT origin → raise :class:`CommsNodeConflictError`. Two hosts
      independently claim the same name; neither has authority over the
      other (operator rename is the only resolution).
    * Existing record that is HIDDEN → the agent stopped and came back,
      possibly on a different port. Un-hide and re-point. A dead record is a
      record of a past placement, not a live claim, so it must not refuse the
      restart that follows it — and with ``spec.a2a.port: auto`` a different
      port is the ORDINARY outcome of a restart, not an edge case.
      (Reproduced on two hosts 2026-08-20: ``business`` live on 19012 behind
      a tombstone at 19033, and ``scitex-dev`` live on 19008 behind an
      11-day-old tombstone at 19003.) Cross-host conflicts are deliberately
      NOT covered by this: that check runs first and still raises, because
      name ownership is not a question a tombstone answers.
    * Existing LIVE record with a different ``(host, a2a_port)`` from the
      SAME origin → raise unless ``replace=True``. Default callers — the
      spec-driven paired write, the channel self-register, the Q4 self-peer
      persistence — do NOT set it; they catch and log, so a real collision
      surfaces in the operator's logs and no record is silently shadowed.

    Parameters
    ----------
    source_host:
        Who is making this claim. ``None`` (every production caller but the
        operator-repair verb) means "this host", read from the store's own
        node id — the same value the primitive stamps into ``_origin``. Pass
        it explicitly only when registering an entry on another host's
        BEHALF, which is what ``sac registry register --source-host`` is for:
        the declared source is then compared against the stored record's
        ``_origin``, so relaying a peer's entry does not read as this host
        claiming the name.

        It is no longer PERSISTED. The column it used to fill was NULL for
        every locally registered row; ``_origin`` records the same fact
        without the writer having to remember it.
    kind:
        Discriminator for the calling path (``spec`` / ``self-peer`` /
        ``manual``). Flows into the error message so the operator sees WHICH
        path tried to overwrite WHICH; not persisted.
    source_path:
        Optional caller-supplied source identifier (the spec file path for
        ``kind="spec"``, the discovered ``agents/<n>/spec.yaml`` path for
        ``kind="self-peer"``, a CLI invocation tag for ``kind="manual"``).
        Surfaces in the error message so the operator can disambiguate by
        path.
    replace:
        Opt-in to overwrite an existing same-origin record with a different
        ``(host, a2a_port)``. Wired by the ``--prefer`` flag. Has no effect
        on the cross-origin conflict — that one ALWAYS raises.
    """
    if not name:
        raise ValueError("register_comms_node: name must be non-empty")
    if not host:
        raise ValueError("register_comms_node: host must be non-empty")
    if not isinstance(a2a_port, int) or isinstance(a2a_port, bool) or a2a_port <= 0:
        raise ValueError(
            f"register_comms_node: a2a_port must be a positive int, got {a2a_port!r}"
        )

    def _write(store: "Store") -> None:
        _register_on(
            store,
            name=name,
            host=host,
            a2a_port=a2a_port,
            source_host=source_host,
            kind=kind,
            source_path=source_path,
            replace=replace,
        )

    # The SHARED handle — never closed here, and reopened once if the
    # connection died under it. See ``state_db_comms_nodes_store`` for both.
    run_with_reconnect(_write)


def _register_on(
    store: "Store",
    *,
    name: str,
    host: str,
    a2a_port: int,
    source_host: str | None,
    kind: RegisterCommsNodeKind,
    source_path: str | None,
    replace: bool,
) -> None:
    """The upsert itself, against an already-open ``store``.

    Split out so :func:`run_with_reconnect` can re-run the WHOLE operation
    on a fresh handle: the read and the write have to happen on the same
    connection, so retrying half of it would be worse than not retrying.
    """
    from scitex_dev.store import ANY_REVISION, NEW_RECORD

    key = {"name": name}
    # include_hidden: a withdrawn record still occupies the identity, so a
    # plain read would say "absent" and the NEW_RECORD insert below would
    # collide with a record the caller cannot see.
    existing = store.get(key, include_hidden=True)
    if existing is None:
        store.put(
            {
                "name": name,
                "host": host,
                "a2a_port": a2a_port,
                "registered_at": time.time(),
            },
            expected_revision=NEW_RECORD,
        )
        return

    _guard_conflict(
        existing,
        name=name,
        host=host,
        a2a_port=a2a_port,
        source_host=source_host or store.node,
        kind=kind,
        source_path=source_path,
        replace=replace,
    )

    if existing.hidden:
        store.unhide(key, expected_revision=ANY_REVISION, actor=ACTOR)
    store.put(
        {
            "name": name,
            "host": host,
            "a2a_port": a2a_port,
            # Carried forward, never restamped. ``registered_at`` is
            # IMMUTABLE: writing time.time() here would make every ordinary
            # refresh a reported MergeConflict, which would turn the
            # loud-conflict channel into noise and bury the one collision it
            # exists to report.
            "registered_at": float(existing.values["registered_at"]),
        },
        expected_revision=ANY_REVISION,
    )


def _guard_conflict(
    existing: "Row",
    *,
    name: str,
    host: str,
    a2a_port: int,
    source_host: str,
    kind: RegisterCommsNodeKind,
    source_path: str | None,
    replace: bool,
) -> None:
    """Raise :class:`CommsNodeConflictError` on a disagreeing claim.

    Hoisted out of :func:`register_comms_node` so the write path reads as the
    short upsert it is, and so the two collision shapes sit next to each
    other where they can be compared.
    """
    stored_host = str(existing.values["host"])
    stored_port = int(existing.values["a2a_port"])
    if stored_host == host and stored_port == a2a_port:
        # Same target — idempotent refresh, nothing to decide.
        return

    origin = str(existing.origin)
    if origin != source_host:
        raise CommsNodeConflictError(
            f"comms_nodes name conflict for {name!r}: "
            f"existing=(host={stored_host!r}, port={stored_port}, "
            f"origin={origin!r}) "
            f"new=(kind={kind!r}, host={host!r}, port={a2a_port}, "
            f"source={source_host!r}, source_path={source_path!r}). "
            f"ADR-0014: names are globally unique. Rename or unregister one "
            f"of them — both hosts read and write the SAME directory now, so "
            f"there is no sync to re-run and no second copy to reconcile."
        )

    if existing.hidden:
        # A tombstone is a record of a past placement, not a live claim.
        return

    if replace:
        # Explicit replace — operator-confirmed via --prefer.
        return

    other_kind = "spec" if kind == "self-peer" else "self-peer pointer"
    raise CommsNodeConflictError(
        f"comms_nodes silent-overwrite refused for {name!r} "
        f"(operator directive 12847, PR L1): "
        f"existing=(host={stored_host!r}, port={stored_port}, "
        f"origin={origin!r}) "
        f"incoming=(kind={kind!r}, host={host!r}, port={a2a_port}, "
        f"source={source_host!r}, source_path={source_path!r}). "
        f"Two registrations for the same name disagree on the "
        f"(host, a2a_port) target. Resolve by either:\n"
        f"  - rerunning the canonical writer with "
        f"`--prefer {kind}` to declare intent (overwrites), or\n"
        f"  - removing/renaming the conflicting {other_kind} "
        f"so a single source owns this name."
    )


def unregister_comms_node(*, name: str) -> bool:
    """Withdraw ``name`` from the directory. ``True`` iff one was live.

    Hides rather than deletes, which is the same soft tombstone the
    ``ended_at`` column expressed — with the difference that made the move
    worth doing: a hide is an OP, so it reaches every host by the same path a
    registration does, where ``INSERT OR IGNORE`` could carry it to none of
    them.

    The record, its values and its whole history stay readable through
    ``include_hidden=True`` and in the oplog. Re-running on an
    already-withdrawn name is a no-op returning ``False``, matching what the
    SQLite ``rowcount == 0`` meant.
    """
    if not name:
        return False

    from scitex_dev.store import ANY_REVISION

    def _withdraw(store: "Store") -> bool:
        key = {"name": name}
        if store.get(key) is None:
            # Absent, or already hidden — either way nothing was live.
            return False
        store.hide(key, expected_revision=ANY_REVISION, actor=ACTOR)
        return True

    return bool(run_with_reconnect(_withdraw))


def rename_comms_node(*, old: str, new: str) -> bool:
    """Move ``old``'s directory entry onto ``new``. ``True`` iff one moved.

    ``name`` is the record IDENTITY, so a rename is not an update: it is one
    record ending and another beginning. This copies the routing tuple onto
    the new identity and withdraws the old one.

    Called from the agent-rename flow, which used to do this with
    ``UPDATE comms_nodes SET name = ?`` as one more ``(table, column)`` pair
    in :data:`.._lifecycle._rename_db.NAME_COLUMNS`. Leaving the pair there
    after the move would have been WORSE than a crash: ``rename_rows`` skips
    tables absent from ``sqlite_master``, so the rename would have reported
    success while the A2A directory kept advertising the OLD name. Peers then
    resolve a name the agent no longer answers to, and the renamed agent is
    unreachable. Withdrawing the old entry matters just as much in the other
    direction — a live directory entry for a name that no longer exists is a
    routing target nobody owns.

    ``registered_at`` is carried forward rather than restamped: a renamed
    agent is the SAME agent, and its registration time is a fact about when
    it joined the graph, not about when it was renamed.

    Idempotent in the useful sense: with nothing live under ``old`` it
    returns ``False`` and writes nothing, so a re-run after a partial rename
    does not clobber the entry already sitting under ``new``.
    """
    if not old or not new or old == new:
        return False

    from scitex_dev.store import ANY_REVISION

    def _move(store: "Store") -> bool:
        row = store.get({"name": old})
        if row is None:
            return False

        # REFUSE a live occupant, loudly. The SQLite path did this for us:
        # ``name`` was the PRIMARY KEY, so ``rename_rows``' UPDATE hit a
        # UNIQUE constraint and ``_rename_db`` turned the IntegrityError into
        # ``DbRenameError("state.db already holds rows for <new>")``. The
        # store has no such constraint — a put on an occupied identity is an
        # ordinary upsert — so without this check a rename onto a name
        # another LIVE agent already answers to would silently repoint that
        # agent's routing entry at this one. Two agents, one directory entry,
        # no error: the renamed agent works and the victim becomes
        # unreachable, which is the failure this table exists to prevent.
        occupant = store.get({"name": new})
        if occupant is not None:
            raise CommsNodeConflictError(
                f"comms_nodes rename refused: {new!r} is already registered "
                f"at host={str(occupant.values['host'])!r} "
                f"port={int(occupant.values['a2a_port'])} "
                f"(origin={str(occupant.origin)!r}), so renaming {old!r} onto "
                f"it would silently take over that agent's routing entry and "
                f"leave it unreachable. Unregister or rename the existing "
                f"{new!r} first."
            )

        moved = {
            "name": new,
            "host": str(row.values["host"]),
            "a2a_port": int(row.values["a2a_port"]),
            "registered_at": float(row.values["registered_at"]),
        }
        # A WITHDRAWN record under ``new`` is taken over rather than refused,
        # and that is a deliberate difference from the SQLite path, which
        # refused this too (a tombstone still occupied the PK). It has to be:
        # renaming back — ``old`` -> ``new`` -> ``old`` — leaves ``old``
        # withdrawn by the first move, so refusing here would make the
        # documented inverse impossible. A withdrawn entry is a record of a
        # past placement, not a live claim, exactly as in
        # :func:`register_comms_node`.
        if store.is_hidden({"name": new}):
            store.unhide({"name": new}, expected_revision=ANY_REVISION, actor=ACTOR)
        store.put(moved, expected_revision=ANY_REVISION)
        store.hide({"name": old}, expected_revision=ANY_REVISION, actor=ACTOR)
        return True

    return bool(run_with_reconnect(_move))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def lookup_comms_node(*, name: str) -> dict[str, Any] | None:
    """Return the LIVE directory entry for ``name`` as a dict, or ``None``.

    Withdrawn (hidden) entries read as absent — for the resolver they are
    equivalent to "not present", which is what filtering ``ended_at IS NULL``
    meant. Callers that need to SEE them use :func:`list_comms_nodes` with
    ``include_ended=True``.
    """
    if not name:
        return None
    row = run_with_reconnect(lambda store: store.get({"name": name}))
    return None if row is None else comms_node_as_dict(row)


def resolve_comms_node_host(*, name: str) -> dict[str, Any] | None:
    """Resolver-shaped lookup for cross-host A2A forwarding.

    Returns ``{host, a2a_port}`` (matching the
    :func:`..state_db_nodes.resolve_node_host` shape) or ``None`` when the
    name is missing or withdrawn. Used by ``resolve_node_host`` as the
    fallback after the ``instances`` lookup misses — and, since the move, it
    answers for agents on OTHER hosts without anything having had to sync
    first, which is the entire point of the migration.
    """
    info = lookup_comms_node(name=name)
    if info is None:
        return None
    return {"host": info["host"], "a2a_port": info["a2a_port"]}


def list_comms_nodes(*, include_ended: bool = False) -> list[dict[str, Any]]:
    """Every directory entry as a list of dicts, ordered by ``name``.

    Default omits withdrawn entries; ``include_ended=True`` returns the full
    directory including tombstones (an operator inspecting what is dead, or
    auditing a name that stopped resolving).

    Ordered by ``name`` ascending, as before — the identity is the name, so
    that order is both deterministic and the one an operator reads by. (The
    grants listing orders by the HLC instead; there a record's position in
    the audit trail IS the information, and here it is not.)
    """
    rows = run_with_reconnect(
        lambda store: store.rows(include_hidden=include_ended)
    )
    return sorted((comms_node_as_dict(row) for row in rows), key=lambda r: r["name"])
