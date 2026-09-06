"""The postgres accessor for the credential tables — narrow on purpose.

ADR-0022 §4 places the general state API in ``scitex_dev.state``
(``dsn_for``, ``write_transaction``, ``store_identity``). That module does
not exist yet, and this domain cannot wait for it: §5.1's whole point is
that tables are being created *now* and a table born without the sync
contract can never be synchronised without a rewrite.

So this file is deliberately the smallest thing that can hold the rows,
written to §4's shape so it lifts out unchanged when the real one lands:

* ``dsn()`` resolves from the environment, **raises rather than guessing**,
  and **refuses to emit 5432** — the ADR's ruling is that any scitex code
  naming 5432 is a defect, so this treats it as one rather than trusting
  every caller to remember.
* the password is never in the DSN; libpq reads ``$PGPASSFILE``.
* the schema is asserted **once per store**, not once per connect. sac's
  ``init_schema``-on-every-``open_db`` habit would become a per-open
  network DDL storm; scitex-cards already hit exactly that.

``psycopg`` is imported lazily. sac's core install has no database driver
and must keep working without one, so a missing driver produces a clear
error at the moment a store is opened rather than at import of the
package that merely *describes* credentials.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from ._model import CredentialDescriptor, CredentialObservation, CredentialPlacement
from ._schema import INDEX_DDL, TABLES, assert_schema_contract

#: The DSN for this host's state database. Per-host postgres on 55432 is
#: the intended topology (ADR-0022 §2); cross-host isolation today is
#: expected, not a bug.
DSN_ENV = "SCITEX_AGENT_CONTAINER_STATE_DSN"

#: Stores whose schema has already been asserted this process, keyed by
#: DSN. "Once per store", not once per connect.
_ASSERTED: set[str] = set()


class StoreTargetNotConfigured(RuntimeError):
    """No state DSN is configured. Servers must not guess (ADR-0022 §4)."""


class ForbiddenPortError(ValueError):
    """A DSN names port 5432. Operator ruling: that is always a defect here."""


class DriverMissingError(RuntimeError):
    """psycopg is not installed in this interpreter."""


def dsn(explicit: str | None = None) -> str:
    """Resolve the state DSN. Never returns a path; never emits 5432.

    Raises rather than defaulting. A credential store that silently
    resolved to *somewhere* is the failure ADR-0022 was written about:
    "a fact was cached into a store whose identity depends on where the
    reader is standing."
    """
    value = explicit or os.environ.get(DSN_ENV)
    if not value:
        raise StoreTargetNotConfigured(
            f"no credential-state DSN: set {DSN_ENV} to this host's postgres "
            f"(port 55432). Refusing to guess a target — a store resolved by "
            f"guessing is how a fleet ends up reading different databases and "
            f"believing the registry was wiped."
        )
    if ":5432/" in value or value.rstrip("/").endswith(":5432"):
        raise ForbiddenPortError(
            f"the configured DSN names port 5432. Operator ruling "
            f"(ADR-0022 §2): 5432 is never used for scitex — any code, spec "
            f"or default naming it is a defect. scitex postgres listens on "
            f"55432."
        )
    if "password=" in value.lower():
        raise ForbiddenPortError(
            "the configured DSN embeds a password. libpq reads $PGPASSFILE; "
            "a password in a DSN lands in process listings and logs."
        )
    return value


def _connect(target: str):
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise DriverMissingError(
            "psycopg (v3) is not installed in this interpreter, so the "
            "credential state store cannot be opened. Install "
            "scitex-agent-container[state]."
        ) from exc
    return psycopg.connect(target, autocommit=False)


def assert_schema(conn, *, target: str) -> None:
    """Create the tables if absent, once per store per process.

    :func:`._schema.assert_schema_contract` runs FIRST and fails closed:
    a table that has drifted out of the §5.1 contract stops the process
    that would have created it, rather than quietly creating rows that
    can never be synchronised.
    """
    if target in _ASSERTED:
        return
    assert_schema_contract()
    with conn.cursor() as cur:
        for _name, ddl, _cls in TABLES:
            cur.execute(ddl)
        for index in INDEX_DDL:
            cur.execute(index)
    conn.commit()
    _ASSERTED.add(target)


def open_store(explicit: str | None = None):
    """Open a connection with the schema asserted. Caller closes it."""
    target = dsn(explicit)
    conn = _connect(target)
    assert_schema(conn, target=target)
    return conn


def _insert(conn, table: str, row: dict[str, Any], *, on_conflict: str) -> None:
    columns = sorted(row)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) {on_conflict}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, row)


def record_descriptor(conn, descriptor: CredentialDescriptor) -> None:
    """Insert a descriptor. ``DO NOTHING`` on conflict — never blind UPDATE.

    ADR-0022 §5.2 prohibits blind ``ON CONFLICT DO UPDATE``: a one-time
    replication between two card stores diverged in both directions, and
    ``DO NOTHING`` was the difference between repair and data loss.
    Updating an existing descriptor is :func:`update_descriptor`, which
    is owner-partitioned.
    """
    _insert(
        conn,
        "credential_descriptor",
        descriptor.to_row(),
        on_conflict="ON CONFLICT ON CONSTRAINT credential_descriptor_ident DO NOTHING",
    )


def record_placement(conn, placement: CredentialPlacement) -> None:
    """Insert a placement. ``DO NOTHING`` on conflict, for the same reason."""
    _insert(
        conn,
        "credential_placement",
        placement.to_row(),
        on_conflict="ON CONFLICT ON CONSTRAINT credential_placement_ident DO NOTHING",
    )


def record_observation(conn, observation: CredentialObservation) -> None:
    """Append an observation. Log class: union, collision means duplicate."""
    _insert(
        conn,
        "credential_observation",
        observation.to_row(),
        on_conflict="ON CONFLICT DO NOTHING",
    )


def update_descriptor(
    conn, *, cred_key: str, origin_node: str, node_id: str, **changes: Any
) -> int:
    """Owner-partitioned UPDATE. Returns rows changed.

    ADR-0022 §5.2 rule 3: a node may ``UPDATE`` only rows it authored.
    The ``origin_node = %(node_id)s`` predicate is that rule, expressed
    where it cannot be forgotten — in the only UPDATE path there is.
    ``revision`` is bumped here because the owner is the only party
    permitted to bump it, and a bump the owner did not make is
    indistinguishable from divergence.
    """
    if origin_node != node_id:
        raise PermissionError(
            f"node {node_id!r} may not update a row authored by "
            f"{origin_node!r}. ADR-0022 §5.2 rule 3: every other node's rows "
            f"are read-only replicas. To change a credential this node does "
            f"not own, ask its owner or hand primacy over deliberately."
        )
    from ._material import assert_no_material

    assert_no_material(changes, what=f"descriptor update {cred_key!r}")
    if not changes:
        return 0
    assignments = ", ".join(f"{k} = %({k})s" for k in sorted(changes))
    params: dict[str, Any] = dict(changes)
    params.update({"cred_key": cred_key, "node_id": node_id})
    sql = (
        f"UPDATE credential_descriptor SET {assignments}, "
        f"revision = revision + 1, updated_at = now() "
        f"WHERE cred_key = %(cred_key)s AND origin_node = %(node_id)s "
        f"AND deleted_at IS NULL"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def _fetch(conn, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        names = [d.name for d in cur.description or []]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def descriptors(conn) -> list[dict]:
    """Every live descriptor."""
    return _fetch(
        conn,
        "SELECT * FROM credential_descriptor WHERE deleted_at IS NULL "
        "ORDER BY cred_key",
    )


def placements_for(conn, node: str) -> list[dict]:
    """Every live placement declared for ``node``."""
    return _fetch(
        conn,
        "SELECT * FROM credential_placement WHERE node = %(node)s "
        "AND deleted_at IS NULL ORDER BY cred_key",
        {"node": node},
    )


def cr001_violations(conn) -> list[dict]:
    """Credentials declaring more than one primary node. Should be empty."""
    from ._schema import CR001_MULTIPLE_PRIMARIES_SQL

    return _fetch(conn, CR001_MULTIPLE_PRIMARIES_SQL)


def divergent_declarations(conn) -> list[dict]:
    """Credentials declared by more than one origin — report, never merge."""
    from ._schema import DIVERGENT_DECLARATIONS_SQL

    return _fetch(conn, DIVERGENT_DECLARATIONS_SQL)


def latest_observations(conn, *, cred_key: str | None = None) -> list[dict]:
    """The newest observation per (cred_key, node)."""
    # The ``::text`` casts are load-bearing, not decoration. postgres
    # cannot infer a parameter's type from ``$1 IS NULL`` alone and
    # raises AmbiguousParameter; the optional-filter idiom needs the
    # type stated. Caught by running against a real database — a fake
    # connection would have asserted the string and reported green.
    sql = """
    SELECT DISTINCT ON (cred_key, node) *
      FROM credential_observation
     WHERE (%(cred_key)s::text IS NULL OR cred_key = %(cred_key)s::text)
     ORDER BY cred_key, node, observed_at DESC
    """
    return _fetch(conn, sql, {"cred_key": cred_key})


def refresh_holders(conn, *, cred_key: str) -> list[str]:
    """Nodes whose latest observation shows them holding refresh material."""
    rows = latest_observations(conn, cred_key=cred_key)
    return sorted(
        str(r["node"]) for r in rows if r.get("holds_refresh_material") is True
    )


def record_many(conn, rows: Iterable[Any]) -> None:
    """Convenience bulk insert dispatching on row type."""
    dispatch = {
        CredentialDescriptor: record_descriptor,
        CredentialPlacement: record_placement,
        CredentialObservation: record_observation,
    }
    for row in rows:
        dispatch[type(row)](conn, row)
