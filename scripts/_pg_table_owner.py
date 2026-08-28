#!/usr/bin/env python3
"""Who OWNS the tables a migration creates — and can the fleet still use them?

MEASURED OUTAGE, 2026-08-28
===========================
``migrate_channel_events_to_postgres.py`` creates its tables on first connect,
so they end up owned by WHOEVER RAN IT. The operator ran it as
``ywatanabe__cli``; ``sac_channel_events`` and ``sac_channel_cursor`` came out
owned by that leaf role with no grants to anybody. Every agent connects as
``ywatanabe__<agent>``, and ``init_channel_schema``'s
``CREATE INDEX IF NOT EXISTS`` requires OWNERSHIP of the table rather than
merely privileges on it — so the fleet's message channel began failing with
``InsufficientPrivilege: must be owner of table sac_channel_events`` about
three minutes later, and stayed broken for six until the tables were reowned
by break-glass.

THE POST-MIGRATION CHECK PASSED THROUGHOUT, because it ran as the MIGRATING
role. A verification performed by the writer cannot see a permission the
writer happens to hold. :func:`consumer_access_problems` therefore asks the
catalog about OTHER roles BY NAME and never about ``current_user``.

THE ROLE TREE, MEASURED ON THE FLEET PRIMARY (2026-08-28)
=========================================================
``ywatanabe__<agent>`` is a member of ``ywatanabe``, which is a member of
``scitex_store_owner``. Every table in ``public`` is owned by
``scitex_store_owner`` and carries a NULL ``relacl`` — no explicit grants at
all. Access works purely by MEMBERSHIP IN THE OWNER. That is why a ``GRANT``
would not have fixed the outage: ``CREATE INDEX`` needs owner rights, and no
grant confers those.

Measured on a throwaway table created by a leaf role, for every other agent
role in the cluster: ``has_table_privilege`` False and
``pg_has_role(<role>, <owner>, 'USAGE')`` False. Against the reowned
``sac_channel_events``: both True. Those two catalog questions are the
instrument this module uses.

NO ROLE NAME IS HARDCODED
=========================
``scitex_store_owner`` is a fact about THIS fleet, not about the script, and a
literal would be wrong on any database provisioned differently — CI's
throwaway included. The intended owner is DERIVED: whatever role owns the rest
of the tables in the target schema is, by construction, the role these tables
must match, because that is the role the fleet's consumers have already been
measured to inherit. An operator can always override it outright.

A SCHEMA WITH NOTHING ELSE IN IT has no convention to be consistent with, so
the answer there is ``current_user`` and no ``SET ROLE`` happens. That is the
virgin-database case, and it is also every ``pg_schema`` test fixture.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

#: Operator override for the role the created tables must end up owned by.
#: The ``--table-owner`` flag wins over it; both beat the derivation.
OWNER_ENV = "SAC_MIGRATION_TABLE_OWNER"

#: The privileges a consumer needs on these tables to do its job. SELECT and
#: INSERT are the SSE read and the publish path; UPDATE is ``mark_delivered``;
#: DELETE is retention, which nothing runs yet and which a consumer that
#: cannot do it would discover only much later.
_NEEDED = ("SELECT", "INSERT", "UPDATE", "DELETE")


def current_role(conn: Any) -> str:
    """The role statements run as RIGHT NOW — after any ``SET ROLE``."""
    return str(conn.execute("SELECT current_user").fetchone()[0])


def role_exists(conn: Any, role: str) -> bool:
    """Is ``role`` a role in this cluster?

    Asked separately because ``pg_has_role`` RAISES ``UndefinedObject`` on an
    unknown name, and a typo'd ``--table-owner`` deserves a sentence rather
    than a traceback.
    """
    return bool(
        conn.execute("SELECT to_regrole(%s) IS NOT NULL", (role,)).fetchone()[0]
    )


def table_owner(conn: Any, table: str) -> str | None:
    """The owner of ``table`` in the current schema, or ``None`` if absent."""
    row = conn.execute(
        "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = %s",
        (table,),
    ).fetchone()
    return None if row is None else str(row[0])


def resolve_intended_owner(
    conn: Any, *, managed: Sequence[str], override: str | None = None
) -> tuple[str, str]:
    """``(role, why)`` — the role ``managed`` must end up owned by.

    Precedence: the explicit override, then the majority owner of the OTHER
    tables in this schema, then ``current_user``.

    THE MANAGED TABLES ARE EXCLUDED FROM THE DERIVATION ON PURPOSE. Reading
    their own owner would make a wrong owner self-perpetuating — the exact
    state the fleet was left in on 2026-08-28, where re-running the migration
    would have cheerfully confirmed ``ywatanabe__cli`` as correct.
    """
    declared = override or os.environ.get(OWNER_ENV, "").strip() or None
    if declared:
        return declared, "declared by --table-owner/" + OWNER_ENV
    row = conn.execute(
        "SELECT pg_get_userbyid(c.relowner) AS owner, COUNT(*) AS n "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
        "AND c.relname <> ALL(%s) "
        "GROUP BY 1 ORDER BY n DESC, owner ASC LIMIT 1",
        (list(managed),),
    ).fetchone()
    if row is not None:
        return str(row[0]), f"owner of {int(row[1])} other table(s) in this schema"
    return current_role(conn), "nothing else in this schema to be consistent with"


def owner_is_reachable(conn: Any, *, owner: str) -> str | None:
    """``None`` if this session could hand a table to ``owner``, else why not.

    Asked BEFORE any DDL, because the answer decides whether this migration
    may run at all: a session that cannot put the tables in the right hands
    must refuse rather than create them in the wrong ones.

    ``ALTER TABLE ... OWNER TO`` needs two things — the ability to ``SET ROLE``
    to the new owner, and CREATE on the table's schema for that owner. Only
    the first is checkable without attempting it; the second surfaces as the
    error :func:`ensure_owner` reports verbatim.
    """
    if owner == current_role(conn):
        return None
    if not role_exists(conn, owner):
        return (
            f"the intended table owner {owner!r} is not a role in this "
            f"cluster. Name an existing role with --table-owner, or create it."
        )
    allowed = conn.execute(
        "SELECT pg_has_role(current_user, %s, 'USAGE')", (owner,)
    ).fetchone()[0]
    if allowed:
        return None
    return (
        f"this session runs as {current_role(conn)!r}, which is not a member "
        f"of {owner!r}, so the tables it creates would be owned by a role the "
        f"other agents cannot use — the 2026-08-28 channel outage exactly. "
        f"Re-run as a member of {owner!r}, or GRANT {owner} TO "
        f"{current_role(conn)};"
    )


def ensure_owner(
    conn: Any, *, managed: Sequence[str], owner: str
) -> tuple[list[str], list[str]]:
    """``(repaired, problems)`` — hand every drifted table to ``owner``.

    CALL IT TWICE: once BEFORE the DDL and once after.

    Before, because a table already owned by the wrong role makes the DDL
    itself fail — ``CREATE INDEX IF NOT EXISTS`` checks ownership before it
    checks existence, which is the statement that took the fleet's channel
    down. After, because a table this run has just CREATED is owned by
    ``current_user`` and has to be handed over.

    REOWNING RATHER THAN ``SET ROLE``-ing FIRST is deliberate. Both need the
    same two privileges, so neither is cheaper; but only this one can repair a
    database that is ALREADY wrong, which is the state every host that ran the
    previous version of this script is in. The cost is a sub-millisecond
    window, inside one connection, in which a freshly created table is owned
    by the migrating role.
    """
    from psycopg import sql

    repaired: list[str] = []
    problems: list[str] = []
    for table in managed:
        actual = table_owner(conn, table)
        if actual is None or actual == owner:
            continue
        try:
            conn.execute(
                sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    sql.Identifier(table), sql.Identifier(owner)
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            problems.append(
                f"{table} is owned by {actual!r} and cannot be handed to "
                f"{owner!r} from this session ({exc}). Re-run as {actual!r} "
                f"— the role that created it — or reown it by break-glass: "
                f"ALTER TABLE {table} OWNER TO {owner};"
            )
        else:
            repaired.append(f"{table}: owner {actual!r} -> {owner!r}")
    return repaired, problems


def _reference_population(
    conn: Any, *, managed: Sequence[str]
) -> tuple[str | None, list[str]]:
    """``(reference_table, roles)`` — who can already use the REST of the store.

    The population is derived from a table this migration did NOT create, so
    it is an INDEPENDENT question from "did we set the owner we intended".
    Deriving it from our own tables would make the check below tautological:
    it would confirm that the roles which can use these tables can use these
    tables.

    Superusers are excluded. They pass every privilege test by fiat, so
    including them would let one superuser row hide every real consumer's
    failure.
    """
    rows = conn.execute(
        "SELECT c.relname, r.rolname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN pg_roles r "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
        "AND c.relname <> ALL(%s) "
        "AND r.rolcanlogin AND NOT r.rolsuper "
        "AND has_table_privilege(r.oid, c.oid, 'SELECT')",
        (list(managed),),
    ).fetchall()
    by_table: dict[str, list[str]] = {}
    for table, role in rows:
        by_table.setdefault(str(table), []).append(str(role))
    if not by_table:
        return None, []
    # The widest population, so the check speaks for as much of the fleet as
    # the database can attest to. Ties break by name, so two runs agree.
    reference = max(sorted(by_table), key=lambda t: len(by_table[t]))
    return reference, sorted(by_table[reference])


def owner_inheritance_problems(
    conn: Any, *, managed: Sequence[str], owner: str
) -> tuple[list[str], str]:
    """``(problems, note)`` — can the fleet's roles ACT AS the intended owner?

    ASKED BEFORE ANY DDL, and that placement is the point. The full check
    below needs the tables to exist, so running only that one would mean
    CREATING the tables in order to discover that nobody can use them — the
    refusal would leave behind the exact hazard it refused. This question
    needs no table: ownership on these two is inherited, not granted, so
    ``pg_has_role(<consumer>, <intended owner>, 'USAGE')`` already decides it.

    It is also the question ``--table-owner`` most needs answered. An operator
    naming a role this session happens to be a member of — its own leaf role,
    say — passes :func:`owner_is_reachable` and still reproduces 2026-08-28
    exactly.
    """
    reference, roles = _reference_population(conn, managed=managed)
    if reference is None:
        return [], (
            "no other table in this schema to derive a consumer population "
            "from — consumer access UNCHECKED, not verified clean"
        )
    short = conn.execute(
        "SELECT r.rolname FROM pg_roles r WHERE r.rolname = ANY(%s) "
        "AND NOT pg_has_role(r.oid, %s::regrole, 'USAGE') ORDER BY 1",
        (roles, owner),
    ).fetchall()
    if not short:
        return [], f"{len(roles)} consumer role(s) inherit {owner!r}"
    names = ", ".join(str(r[0]) for r in short)
    return [
        f"{len(short)} of the {len(roles)} role(s) that can use {reference!r} "
        f"cannot act as {owner!r}: {names}. Tables owned by it would be "
        f"unusable to them through init_channel_schema's CREATE INDEX IF NOT "
        f"EXISTS, which needs OWNER rights and which no GRANT supplies."
    ], f"consumer population drawn from {reference!r}"


def consumer_access_problems(
    conn: Any, *, managed: Sequence[str]
) -> tuple[list[str], str]:
    """``(problems, note)`` — can the roles that use the store use THESE tables?

    ASKED ABOUT OTHER ROLES, NEVER ABOUT ``current_user``. The writer's own
    access is exactly what the 2026-08-28 verification measured, and it was
    true while the fleet was down.

    Two questions per role, because the outage needed both to be answered:

    * ``has_table_privilege`` — can it read and write the rows (the DML half);
    * ``pg_has_role(role, <table owner>, 'USAGE')`` — can it act as the
      table's owner, which is what ``CREATE INDEX IF NOT EXISTS`` in
      ``init_channel_schema`` demands of every agent that opens a connection.
      This is the half that actually broke, and no GRANT can supply it.

    An empty population is REPORTED, not treated as clean. A schema holding
    nothing but these tables cannot answer the question, and "nobody
    complained" from a database with nobody in it is not a pass.
    """
    reference, roles = _reference_population(conn, managed=managed)
    if reference is None:
        return [], (
            "no other table in this schema to derive a consumer population "
            "from — consumer access UNCHECKED, not verified clean"
        )
    problems: list[str] = []
    for table in managed:
        # A table that does not exist cannot be unusable. Skipping rather than
        # letting ``regclass`` raise keeps this callable on a half-built
        # schema, which is where a diagnosis is most likely to be wanted.
        if table_owner(conn, table) is None:
            continue
        privs = " AND ".join(
            f"has_table_privilege(r.oid, %s::regclass, '{p}')" for p in _NEEDED
        )
        short = conn.execute(
            "SELECT r.rolname FROM pg_roles r WHERE r.rolname = ANY(%s) AND NOT ("
            f"{privs} AND pg_has_role(r.oid, "
            "(SELECT c.relowner FROM pg_class c WHERE c.oid = %s::regclass), "
            "'USAGE')) ORDER BY 1",
            (roles, *([table] * len(_NEEDED)), table),
        ).fetchall()
        if short:
            names = ", ".join(str(r[0]) for r in short)
            problems.append(
                f"{table} (owner {table_owner(conn, table)!r}) is NOT usable by "
                f"{len(short)} of the {len(roles)} role(s) that can use "
                f"{reference!r}: {names}. Those roles connect to this table "
                f"through init_channel_schema, whose CREATE INDEX IF NOT "
                f"EXISTS needs OWNER rights — a GRANT will not fix it."
            )
    return problems, (
        f"consumer access checked for {len(roles)} role(s) drawn from "
        f"{reference!r}"
    )
