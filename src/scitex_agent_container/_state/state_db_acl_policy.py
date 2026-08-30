"""Phase-3 capsule-isolation policy — on PostgreSQL only (2026-08-28).

The per-agent ACL record :func:`.._listen._acl.check_send_acl` and
:func:`.._listen._acl.check_spawn` consult on every gated call. Written
at ``agent_start`` from the loaded ``spec.comms`` / ``spec.lineage``
blocks (:func:`.._lifecycle._spawn_gate.persist_acl_policy`) and
re-published by ``sac agents refresh-acl``. The verbs below are
re-exported from :mod:`.state_db_nodes`, so the existing import surface
is unchanged::

    from scitex_agent_container._state.state_db_nodes import (
        record_comms_policy, read_comms_policy, sender_target_relationship,
    )

WHY THIS MODULE MOVED
=====================
Operator ruling, restated 2026-08-28: 「スクライトなんて全部絶滅させて
ください」. The fleet is MULTI-HOST and a state file per host means a
different ACL per host — measured on scitex-compute-04 2026-08-11, where
the in-container per-agent shard held NO policy row for anybody while the
bare-host file held them all, so every authority gate resolved the empty
set and answered 403. A per-host state.db is that outage's storage layer.

The move ADOPTS :mod:`scitex_dev.store`, the fleet's own primitive, whose
``resolve_target`` is exactly two steps (``SCITEX_STORE_DSN`` or the
per-host PostgreSQL) with NO local-file fallback. A host whose PostgreSQL is
unreachable raises ``StoreTargetError`` naming the DSN it could not
reach. Fail fast, fail loud, no fallbacks.

THAT LOUDNESS IS THE SECURITY PROPERTY, and it is why this table is not
"just another migration". A lost write in the diary costs an
OBSERVATION. A lost write here is a DENIAL or a WRONGFUL ALLOW: the gate
reads whatever record it finds, and :func:`read_comms_policy` answers
with all-allow defaults when it finds none. Silence is indistinguishable
from permission, so the store must raise rather than return empty.

``db_path`` IS GONE from every policy signature. It named a file;
there is no file. Test isolation now comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema — the ``pg_schema`` fixture —
which is better isolation than a temp path was, because it exercises the
real resolver.

ONE FUNCTION HERE DID NOT MOVE, AND KEEPS ``db_path``
=====================================================
:func:`sender_target_relationship` reads ``lineage``, a DIFFERENT table
with a different owner and its own migration ahead of it. It now lives in
:mod:`.state_db_lineage_rel` and is re-exported here so no import
changes. See that module for the full argument.

REMOVAL IS ``hide``, NEVER A DELETE
===================================
The original table had no delete — but the rename path did
``UPDATE node_comms_policy SET name = ?``, which under an IDENTITY field
is not an update at all: it is one record ending and another beginning.
:func:`rename_comms_policy` therefore copies the values onto the new name
and RETIRES the old one with :meth:`Store.hide`. Hiding keeps the retired
record and its whole history readable through ``include_hidden=True`` and
in the oplog, so "this agent never had a policy" and "this agent's policy
was retired at a rename" stay different answers. For an ACL that
difference is the whole audit.

The schema, the store factory, the ``group_names`` codec and the
validators live in :mod:`.state_db_acl_policy_store` — same reason the
lineage reader has its own file, the per-file line cap.
"""

from __future__ import annotations

import time
from typing import Any

from .state_db_acl_policy_store import (
    ACTOR as _ACTOR,
)
from .state_db_acl_policy_store import (
    POLICY_STORE,
    decode_policy,
    join_group_names,
    policy_schema,
    split_group_names,
    validate_policy,
)
from .state_db_acl_policy_store import (
    open_policy_store as _open,
)
from .state_db_lineage_rel import sender_target_relationship

__all__ = [
    "DEFAULT_COMMS_POLICY",
    "POLICY_STORE",
    "apply_may_spawn_gate",
    "comms_policy_row_exists",
    "read_comms_policy",
    "record_comms_policy",
    "rename_comms_policy",
    "retire_comms_policy",
    "sender_target_relationship",
]


DEFAULT_COMMS_POLICY: dict[str, Any] = {
    "outbound_siblings": "allow",
    "outbound_parent": "allow",
    "inbound_siblings": "allow",
    "inbound_parent": "allow",
    "lineage_group": "",
    "may_spawn": True,
    # Group-based ACL (operator 2026-06-25): the agent's PRIMARY named
    # group — the single bucket the default-ACL mesh resolves through.
    # "" (ungrouped) is the default; absence is byte-equivalent to the
    # pre-group-name behaviour (same-group allow needs a non-empty match).
    "group_name": "",
    # EVERY named group the spec's ``metadata.labels`` lists (incident
    # 2026-08-10). The AUTHORITY gates read this set, not the primary
    # above, so an agent authored as ``groups: [generalist, developer]``
    # is a developer regardless of list order. Empty tuple on a record
    # written before the field existed — ``resolve_group_names`` unions
    # it with ``group_name``, so that record keeps its old meaning.
    "group_names": (),
}


def apply_may_spawn_gate(
    *,
    caller: str | None,
    base: tuple[bool, str | None],
) -> tuple[bool, str | None]:
    """Layer ``spec.lineage.may_spawn=false`` on top of the global policy.

    Evaluated AFTER the global policy result: a deny stays a deny
    (existing reason preserved). If the global path allowed but the
    caller has ``may_spawn=False`` in its persisted policy, the allow
    flips to a per-spec deny with a clear reason. Admin path
    (``caller=None``) has no name to look up — kept untouched, so
    operator-launched starts never trip this.
    """
    if not base[0]:
        return base
    if not caller:
        return base
    policy = read_comms_policy(name=caller)
    if not policy["may_spawn"]:
        return (
            False,
            (
                f"spawn denied: caller {caller!r} has "
                "spec.lineage.may_spawn=false in its agent definition; "
                "the per-spec deny survives global-policy relaxation."
            ),
        )
    return base


def record_comms_policy(
    *,
    name: str,
    outbound_siblings: str = "allow",
    outbound_parent: str = "allow",
    inbound_siblings: str = "allow",
    inbound_parent: str = "allow",
    lineage_group: str = "",
    may_spawn: bool = True,
    group_name: str = "",
    group_names=None,
) -> None:
    """Upsert the Phase-3 per-spec ACL policy for ``name``.

    Called from core ``agent_start`` after the spawn-gate runs, so the
    record always reflects the *current* ``spec.comms`` /
    ``spec.lineage`` blocks on disk. A re-start refreshes it in place (a
    spec edit becomes live on the next start without manual surgery).

    EVERY field is written on every call, including the ones the caller
    left at their defaults. ``Store.put`` is a PARTIAL update — absent
    fields are left alone — so writing the full record is what preserves
    the original ``INSERT ... ON CONFLICT DO UPDATE SET <all columns>``
    semantics. Dropping to a partial write would let a previous
    ``outbound_siblings="deny"`` survive a spec edit that removed it,
    which is a stale ACL wearing a fresh timestamp.

    ``group_name`` is the PRIMARY group (the default-ACL mesh bucket);
    ``group_names`` is EVERY group the spec names (the authority set).
    Both are projections of the same ``metadata.labels`` and are written
    together — that is what keeps them from disagreeing. ``group_names``
    defaults to ``None``, which stores the PRIMARY alone, so an existing
    caller that passes only ``group_name`` keeps its exact old meaning.

    A record RETIRED by :func:`retire_comms_policy` (or by a rename) is
    un-hidden here: re-publishing a policy for a name is exactly the
    operation that should bring it back, and leaving it hidden would deny
    an agent that had just been re-registered.

    Raises :class:`ValueError` on an empty name or out-of-domain values
    (the parser/validator already reject these — defence-in-depth).
    """
    validate_policy(
        name=name,
        outbound_siblings=outbound_siblings,
        outbound_parent=outbound_parent,
        inbound_siblings=inbound_siblings,
        inbound_parent=inbound_parent,
        lineage_group=lineage_group,
        may_spawn=may_spawn,
        group_name=group_name,
    )
    primary = group_name.strip()
    # A caller that names only the primary keeps its old meaning: the
    # stored set is {primary}. A caller that names the full set gets it
    # stored verbatim, with the primary folded in so the set is never a
    # strict subset of what the mesh already resolves.
    if group_names is None:
        encoded_groups = join_group_names([primary])
    elif isinstance(group_names, str):
        # Reject BEFORE the splat below, which would silently expand a
        # string into its characters and store those as group names.
        encoded_groups = join_group_names(group_names)
    else:
        encoded_groups = join_group_names([*group_names, primary])

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        key = {"name": name}
        if store.is_hidden(key):
            store.unhide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        store.put(
            {
                "name": name,
                "outbound_siblings": outbound_siblings,
                "outbound_parent": outbound_parent,
                "inbound_siblings": inbound_siblings,
                "inbound_parent": inbound_parent,
                "lineage_group": lineage_group,
                "may_spawn": bool(may_spawn),
                "group_name": primary,
                "group_names": encoded_groups,
                "updated_at": time.time(),
            },
            # ANY_REVISION, not NEW_RECORD: this verb is an UPSERT by
            # contract — every agent_start re-publishes the same identity,
            # and NEW_RECORD would raise on the second start of every
            # agent in the fleet.
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()


def read_comms_policy(*, name: str) -> dict[str, Any]:
    """Return the per-spec ACL policy for ``name``, or defaults if absent.

    A missing record yields :data:`DEFAULT_COMMS_POLICY` so the
    "no-row" vs "row-with-default-values" distinction is invisible to
    callers. Defaults are byte-equivalent to pre-Phase-3 behaviour.

    A RETIRED (hidden) record also reads as absent — the same answer for
    the same reason: the gate wants the policy in force NOW, and a
    retired policy is not in force.

    That invisibility is correct for POLICY EVALUATION — a caller asking
    "may this agent spawn?" wants an answer, not a lecture about records.
    It is wrong for DIAGNOSIS: when an ACL refuses, "this agent is
    registered and ungrouped" and "this store has never heard of this
    agent" are different facts and the operator needs to know which.
    Use :func:`comms_policy_row_exists` for that; do NOT infer it from
    a returned value equal to the defaults, which is ambiguous by design.
    """
    if not name:
        return dict(DEFAULT_COMMS_POLICY)
    store = _open()
    try:
        row = store.get({"name": name})
    finally:
        store.close()
    if row is None:
        return dict(DEFAULT_COMMS_POLICY)
    return decode_policy(row.values)


def comms_policy_row_exists(*, name: str) -> bool:
    """True iff THIS store holds a LIVE policy record for ``name``.

    The narrow question :func:`read_comms_policy` deliberately hides, and
    the one a denial message needs.

    INCIDENT 2026-08-09: three agents were refused ``host_exec`` with
    "caller '<name>' resolves to group ''". That message asserts one
    cause — you are registered and ungrouped — when the truth was the
    other: the caller was being looked up in a store that had no row for
    it at all. Both produce the empty string, at two layers
    (``resolve_group_name`` collapses them, and so does this module's
    ``read_comms_policy``), each documented as intended. So the message
    sent three readers after their group labels instead of after WHICH
    DATABASE was consulted, and cost about fifteen minutes.

    This does NOT change any decision. Both cases still deny, and deny
    for the same reason. It exists so the denial can say which one it
    was.

    LIVE, not merely present: a retired record answers ``False`` here,
    matching what :func:`read_comms_policy` does with it. The retired
    record stays readable with ``include_hidden=True`` for anyone
    auditing what changed — that is what hiding is for — but a policy
    that is not in force must not make a denial claim the agent is
    registered.
    """
    if not name:
        return False
    store = _open()
    try:
        return store.get({"name": name}) is not None
    finally:
        store.close()


def retire_comms_policy(*, name: str) -> bool:
    """Withdraw ``name``'s policy record. ``True`` iff one was live.

    Hides rather than deletes — the store's only removal. The record,
    its values and its whole history stay readable through
    ``include_hidden=True`` and in the oplog, while every default read
    treats it as absent. So an ACL audit can still answer "did this agent
    ever hold a policy, and what was in it?" after the agent is gone,
    which a ``DELETE`` could not.
    """
    if not name:
        return False

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        key = {"name": name}
        if store.get(key) is None:
            # Absent, or already retired — either way nothing was live.
            return False
        store.hide(key, expected_revision=ANY_REVISION, actor=_ACTOR)
        return True
    finally:
        store.close()


def rename_comms_policy(*, old: str, new: str) -> bool:
    """Move ``old``'s policy onto ``new``. ``True`` iff a record moved.

    ``name`` is the record IDENTITY, so a rename is not an update: it is
    one record ending and another beginning. This copies every stored
    value onto the new identity and RETIRES the old one.

    Called from the agent-rename flow, which used to do this with
    ``UPDATE node_comms_policy SET name = ?`` across the original table.
    Miss it and the ACL gate holds no policy for the live name, so the
    renamed agent resolves to NO named group and every authority gate
    denies it — the 2026-08-10 shape reached by a different route.
    Retiring the old name matters just as much in the other direction: a
    live policy under a name that no longer exists is a standing
    authorisation nobody owns.

    Idempotent in the useful sense: with nothing live under ``old`` it
    returns ``False`` and writes nothing, so a re-run after a partial
    rename does not clobber the record already sitting under ``new``.
    """
    if not old or not new or old == new:
        return False

    from scitex_dev.store import ANY_REVISION

    store = _open()
    try:
        row = store.get({"name": old})
        if row is None:
            return False
        # Filtered to the SCHEMA's own fields rather than passed straight
        # through: ``put`` looks every key up in ``schema.fields``, so any
        # future bookkeeping key appearing in ``row.values`` would turn a
        # rename into a KeyError at the worst possible moment.
        fields = policy_schema().fields
        moved = {k: v for k, v in row.values.items() if k in fields}
        moved["name"] = new
        moved["updated_at"] = time.time()
        if store.is_hidden({"name": new}):
            store.unhide({"name": new}, expected_revision=ANY_REVISION, actor=_ACTOR)
        store.put(moved, expected_revision=ANY_REVISION)
        store.hide({"name": old}, expected_revision=ANY_REVISION, actor=_ACTOR)
        return True
    finally:
        store.close()


# Kept importable from here because the migration script and the tests
# reach for them by the same names the grants/diary ports established.
_split_group_names = split_group_names
_join_group_names = join_group_names
