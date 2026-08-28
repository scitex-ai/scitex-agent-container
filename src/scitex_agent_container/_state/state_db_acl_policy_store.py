"""The ``node_comms_policy`` store — schema, connection, codec, validators.

Split out of :mod:`.state_db_acl_policy` (which stays the import surface
everything else uses) to keep both files under the per-file line cap.
This half holds nothing a caller outside ``_state`` should reach for: the
schema declaration, the ``Store`` factory, the ``group_names`` encoding,
and the two pure translation helpers the public verbs share.

WRITER POLICY: ``MULTI_WRITER``, DELIBERATELY
=============================================
A policy record has no single stable owner. It is written at
``agent_start`` on whichever host is running the agent — and the fleet
relocates agents between hosts — re-published fleet-wide by ``sac agents
refresh-acl`` from wherever the operator happens to be, and bulk-imported
from peers by :func:`.state_db_export.import_state`. Under
``SINGLE_WRITER`` the first refresh-acl issued from another host would be
refused, and a refused ACL write is a STALE ACL row: the agent keeps the
groups it used to have, or loses the ones it just gained. Both directions
are privilege bugs, and neither raises anywhere a human is looking.

(:mod:`.._store_plugin` declared ``SINGLE_WRITER`` for this table before
the move, back when the row was modelled as owned by the agent's own
host. That declaration is updated in the same commit rather than left to
disagree with the live store.)

UPSERT, NOT APPEND — SO THE FIELDS ARE ``LAST_WRITER_WINS``, NOT IMMUTABLE
=========================================================================
The diary and the grants stores hold historical facts and their fields
are ``IMMUTABLE``. This record is not a fact about an event; it is a
PROJECTION of ``spec.yaml`` (ADR-0022: "both writers derive it from the
spec"), and re-publishing it after a spec edit is the entire reason it
exists. ``IMMUTABLE`` here would make a spec edit unpublishable — the
newest write really is the best answer, so ``LAST_WRITER_WINS`` is
honest.

``updated_at`` is the exception: ``MergeRule.MAX``, so a late-arriving
stale replica cannot walk the record's own clock backwards.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

__all__ = [
    "ACTOR",
    "POLICY_STORE",
    "open_policy_store",
]

#: Logical store name. Renders as four physical tables
#: (``node_comms_policy_rows``, ``_oplog``, ``_identity``, ``_cursor``).
POLICY_STORE = "node_comms_policy"

ACTOR = "scitex-agent-container"

#: The domains the validators enforce, named once so the error messages
#: and the schema cannot drift apart.
ALLOW_DENY = ("allow", "deny")
LINEAGE_GROUPS = ("", "solitary")


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _setting(kind: Any) -> Any:
    """A projection of the spec — the newest write IS the best answer.

    See the module docstring for why this is ``LAST_WRITER_WINS`` where
    the diary and grants stores are ``IMMUTABLE``.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=True,
        merge=MergeRule.LAST_WRITER_WINS,
        indexed=False,
    )


def policy_schema() -> Any:
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    return Schema(
        name=POLICY_STORE,
        fields={
            # The agent name IS the identity, exactly as the SQLite
            # PRIMARY KEY treated it.
            "name": _ident(FieldKind.TEXT),
            "outbound_siblings": _setting(FieldKind.TEXT),
            "outbound_parent": _setting(FieldKind.TEXT),
            "inbound_siblings": _setting(FieldKind.TEXT),
            "inbound_parent": _setting(FieldKind.TEXT),
            "lineage_group": _setting(FieldKind.TEXT),
            # BOOL, not the SQLite 0/1 INTEGER. The column was a boolean
            # wearing an integer's clothes; the store has the type.
            "may_spawn": _setting(FieldKind.BOOL),
            "group_name": _setting(FieldKind.TEXT),
            # Still the comma-separated encoding, deliberately. JSON would
            # be tidier and would change the wire format of an ACL input
            # inside a storage migration, which is how a breaking change
            # rides along unnoticed. Same encoding, same validators, same
            # comma rejection.
            "group_names": _setting(FieldKind.TEXT),
            "updated_at": FieldPolicy(
                kind=FieldKind.REAL,
                role=FieldRole.DATA,
                required=True,
                # MAX, not LAST_WRITER_WINS: a stale replica arriving late
                # must not walk this record's clock backwards.
                merge=MergeRule.MAX,
                indexed=False,
            ),
        },
    )


def open_policy_store() -> "Store":
    """Open the policy store. RAISES if PostgreSQL is unreachable.

    Open-and-close per call mirrors the old ``with open_db(...)`` shape.
    Raising is the security property: :func:`.read_comms_policy` answers
    with all-allow defaults when it finds no record, so a store that
    returned empty instead of raising would read as PERMISSION.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = policy_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        # MULTI_WRITER — see the module docstring. A refused ACL write is
        # a stale ACL record, and a stale ACL record is a privilege bug.
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


def split_group_names(raw: str) -> tuple[str, ...]:
    """Decode the comma-separated ``group_names`` value into a tuple.

    Blank / whitespace-only members are dropped, so ``""`` decodes to the
    empty tuple and a trailing comma is harmless.
    """
    return tuple(part.strip() for part in str(raw or "").split(",") if part.strip())


def join_group_names(groups) -> str:
    """Encode an iterable of group names for the ``group_names`` value.

    De-duplicated and SORTED, so the stored string is deterministic for a
    given set (the value answers a MEMBERSHIP question — order carries
    no meaning, and a stable encoding keeps diffs and denial messages
    readable). Blank members are dropped.

    Raises :class:`ValueError` on a name containing a comma: the encoding
    is comma-separated, so accepting one would silently split a single
    group into two. Fail loudly rather than corrupt an ACL input.
    """
    if groups is None:
        return ""
    if isinstance(groups, str):
        raise ValueError(
            "group_names must be an iterable of group names, not a bare "
            f"string ({groups!r}) — pass e.g. ['developer']"
        )
    out: set[str] = set()
    for item in groups:
        if item is None:
            continue
        trimmed = str(item).strip()
        if not trimmed:
            continue
        if "," in trimmed:
            raise ValueError(
                f"group name {trimmed!r} contains a comma; the group_names "
                "value is comma-separated and cannot encode it"
            )
        out.add(trimmed)
    return ",".join(sorted(out))


def validate_policy(
    *,
    name: str,
    outbound_siblings: str,
    outbound_parent: str,
    inbound_siblings: str,
    inbound_parent: str,
    lineage_group: str,
    may_spawn: bool,
    group_name: str,
) -> None:
    """Reject out-of-domain values BEFORE any store is opened.

    Defence-in-depth — the YAML parser/validator already rejects these.
    Hoisted out of :func:`.record_comms_policy` so a bad value costs no
    connection, and so the refusal is identical whether or not
    PostgreSQL happens to be reachable.
    """
    if not name:
        raise ValueError("record_comms_policy: name must be non-empty")
    for field, value in (
        ("outbound_siblings", outbound_siblings),
        ("outbound_parent", outbound_parent),
        ("inbound_siblings", inbound_siblings),
        ("inbound_parent", inbound_parent),
    ):
        if value not in ALLOW_DENY:
            raise ValueError(f"{field} must be 'allow' or 'deny', got {value!r}")
    if lineage_group not in LINEAGE_GROUPS:
        raise ValueError(
            f"lineage_group must be '' or 'solitary', got {lineage_group!r}"
        )
    if not isinstance(may_spawn, bool):
        raise ValueError(f"may_spawn must be a bool, got {type(may_spawn).__name__}")
    if not isinstance(group_name, str):
        raise ValueError(f"group_name must be a str, got {type(group_name).__name__}")


def decode_policy(values: Any) -> dict[str, Any]:
    """Turn one stored record's values into the caller-facing dict.

    Every field is read through a ``get`` carrying the pre-Phase-3
    default, so a record written before a field existed reads exactly as
    it did under the SQLite column default rather than raising a
    ``KeyError`` inside an ACL gate.
    """
    return {
        "outbound_siblings": str(values.get("outbound_siblings") or "allow"),
        "outbound_parent": str(values.get("outbound_parent") or "allow"),
        "inbound_siblings": str(values.get("inbound_siblings") or "allow"),
        "inbound_parent": str(values.get("inbound_parent") or "allow"),
        "lineage_group": str(values.get("lineage_group") or ""),
        "may_spawn": bool(values.get("may_spawn", True)),
        "group_name": str(values.get("group_name") or ""),
        "group_names": split_group_names(values.get("group_names") or ""),
    }
