"""The ONE declaration of how a heartbeat lands on an ``instances`` row.

WHY THIS MODULE EXISTS
======================
The heartbeat field set used to be spelled out in four places: the writer's
field-to-column map, the writer's optional-field set, and the two schema
declarations (``_store_plugin.INSTANCES`` and
``state_store_instances_store.instances_schema``). They drifted. The writer
mapped ``session_jsonl_delta_bytes``, ``subagent_jsonl_delta_bytes``,
``nonce_challenge`` and ``nonce_echo`` to columns neither schema declared, so
``Store.put`` raised ``KeyError`` and every lease renewal was dropped
(measured 2026-11-18 on compute-01/03/04: 14,900 consecutive failures on
compute-03). Fixing one name at a time only moved the failure to the next
undeclared one.

Every consumer now DERIVES from :data:`HEARTBEAT_COLUMN_SPECS`:

* the writer's map and optional set (``state_store_instances_heartbeat``),
* the heartbeat block of BOTH schemas (:func:`heartbeat_field_policies`),
* the evolving-field list that gates launch on schema evolution
  (``state_store_instances_store.HEARTBEAT_LEASE_FIELDS``).

So a column the writer can emit is, by construction, a column both schemas
declare. Adding a heartbeat field is one row here.

TYPES follow the wire contract (``_listen/_observation_contract``): the byte
deltas are ``FiniteFloat | None`` (REAL), the nonces ``str | None`` (TEXT).
Every heartbeat column merges LAST_WRITER_WINS: a host writes only its own
row (``host`` is in the identity, SINGLE_WRITER), so the rule only ever
orders that host's own successive beats.

``kind`` is a ``FieldKind`` member NAME rather than the enum so importing this
table does not import ``scitex_dev.store``; :func:`heartbeat_field_policies`
resolves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "HEARTBEAT_COLUMN_SPECS",
    "HEARTBEAT_COLUMNS",
    "HEARTBEAT_LEASE_COLUMNS",
    "NULL_READS_AS",
    "OPTIONAL_HEARTBEAT_FIELDS",
    "HeartbeatColumn",
    "heartbeat_field_policies",
]


@dataclass(frozen=True)
class HeartbeatColumn:
    """One heartbeat field and the ``instances`` column that stores it.

    ``optional`` — a VALID beat may omit the field (``validate_heartbeat``
    never requires it). It is then stored as None, so a beat that drops a
    field clears the previous value instead of leaving it stale on the row.
    ``null_reads_as`` — the value a stored NULL reads back as when the row is
    turned into a beat again (``_heartbeat_from_instance``).
    """

    field: str
    column: str
    kind: str
    optional: bool = False
    null_reads_as: Any = None


HEARTBEAT_COLUMN_SPECS: tuple[HeartbeatColumn, ...] = (
    HeartbeatColumn("boot_id", "heartbeat_boot_id", "TEXT"),
    HeartbeatColumn("seq", "heartbeat_seq", "INTEGER"),
    HeartbeatColumn("state", "heartbeat_state", "TEXT"),
    HeartbeatColumn("agent_id", "heartbeat_agent_id", "TEXT"),
    HeartbeatColumn("spec_id", "heartbeat_spec_id", "TEXT"),
    HeartbeatColumn("runtime", "heartbeat_runtime", "TEXT"),
    HeartbeatColumn("harness", "heartbeat_harness", "TEXT"),
    HeartbeatColumn("engine", "heartbeat_engine", "TEXT"),
    HeartbeatColumn("model", "heartbeat_model", "TEXT"),
    HeartbeatColumn("session_id", "heartbeat_session_id", "TEXT"),
    HeartbeatColumn("monotonic_ns", "heartbeat_monotonic_ns", "INTEGER"),
    HeartbeatColumn("observed_at", "heartbeat_observed_at", "REAL"),
    HeartbeatColumn("progress_at", "heartbeat_progress_at", "REAL"),
    # Writers no longer send it (HEARTBEAT SPEC 2026-10-08), but
    # validate_heartbeat still accepts it and enforces its monotonicity
    # against the PREVIOUS beat, which is read back off this column.
    HeartbeatColumn("progress_seq", "heartbeat_progress_seq", "INTEGER", optional=True),
    # Work evidence: the only heartbeat content the verdict may read.
    HeartbeatColumn(
        "session_jsonl_delta_bytes",
        "heartbeat_session_jsonl_delta_bytes",
        "REAL",
        optional=True,
    ),
    HeartbeatColumn(
        "subagent_jsonl_delta_bytes",
        "heartbeat_subagent_jsonl_delta_bytes",
        "REAL",
        optional=True,
    ),
    # Liveness confirmation; absent while the writer is not enrolled.
    HeartbeatColumn(
        "nonce_challenge", "heartbeat_nonce_challenge", "TEXT", optional=True
    ),
    HeartbeatColumn("nonce_echo", "heartbeat_nonce_echo", "TEXT", optional=True),
    HeartbeatColumn(
        "card_id", "heartbeat_card_id", "TEXT", optional=True, null_reads_as=""
    ),
    HeartbeatColumn(
        "card_role", "heartbeat_card_role", "TEXT", optional=True, null_reads_as=""
    ),
    HeartbeatColumn("lease_expires_at", "lease_expires_at", "REAL"),
)

#: Heartbeat field -> ``instances`` column.
HEARTBEAT_COLUMNS: dict[str, str] = {
    spec.field: spec.column for spec in HEARTBEAT_COLUMN_SPECS
}

#: Fields a valid beat may omit; stored as None when absent.
OPTIONAL_HEARTBEAT_FIELDS: frozenset[str] = frozenset(
    spec.field for spec in HEARTBEAT_COLUMN_SPECS if spec.optional
)

#: Heartbeat field -> value a stored NULL reads back as (only where not None).
NULL_READS_AS: dict[str, Any] = {
    spec.field: spec.null_reads_as
    for spec in HEARTBEAT_COLUMN_SPECS
    if spec.null_reads_as is not None
}

#: Every heartbeat column, in declaration order.
HEARTBEAT_LEASE_COLUMNS: tuple[str, ...] = tuple(
    spec.column for spec in HEARTBEAT_COLUMN_SPECS
)


def heartbeat_field_policies(data: Callable[[Any, Any], Any]) -> dict[str, Any]:
    """The heartbeat block of an ``instances`` schema, as ``{column: policy}``.

    ``data(kind, merge)`` is the caller's own DATA-field policy constructor,
    so each schema keeps building its policies the way it builds the rest.
    """
    from scitex_dev.store import FieldKind, MergeRule

    return {
        spec.column: data(FieldKind[spec.kind], MergeRule.LAST_WRITER_WINS)
        for spec in HEARTBEAT_COLUMN_SPECS
    }
