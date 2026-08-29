#!/usr/bin/env python3
"""Enumerate every relation a bare, unqualified row count could be mistaken for.

MEASURED on the fleet primary, 2026-08-28, the night ``instances`` moved to
PostgreSQL (#1258): a ``sac_``-prefixed physical table of the exact same
shape — declared by ``_store_plugin`` under its own classification
namespace, a DIFFERENT thing from the store's bare table — already existed
ALONGSIDE the one the store actually opens. Measured EMPTY on three tables
that same night:

    incarnations_rows 444   vs   sac_incarnations_rows 0
    lineage_rows       20   vs   sac_lineage_rows      0
    instances_rows      ?   vs   sac_instances_rows    0

A verifier that counts the wrong name reads a plausible-looking zero and can
file a WORKING migration as silent data loss — nearly happened on the third
one. This module never guesses which name is right: it reports every
relation that could be confused for it — schema-qualified, with its owner
and row count — and leaves the "is this ambiguous" call to the caller via
:func:`ambiguous`, rather than silently averaging or preferring one.

Generic on purpose: the same pair-of-names hazard was measured on THREE
tables in one night, not one. Nothing here is instances-specific; it is
adopted by ``migrate_instances_to_postgres.py`` first because that is the
migration this incident was measured against.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["Candidate", "candidate_relations", "find_authoritative", "ambiguous"]


class Candidate(dict):
    """One relation's identity + count, as a dict for easy printing/testing.

    Keys: ``schema``, ``table``, ``qualified`` (``schema.table``), ``owner``,
    ``count``. A ``dict`` subclass rather than a dataclass so a caller can
    keep treating a candidate as a plain mapping without importing this
    module's types.
    """


def candidate_relations(
    conn: Any, *, table: str, prefixes: Sequence[str] = ("", "sac_")
) -> list[Candidate]:
    """Every ordinary table named ``<prefix><table>``, in THIS CONNECTION'S
    OWN SCHEMA — ``current_schema()``, the one an unqualified reference
    would actually resolve into.

    Scoped deliberately, not searched cluster-wide. Postgres itself only
    ever considers ``search_path`` schemas when resolving an unqualified
    name, so a same-named table sitting in a schema this connection would
    never look in could not be mistaken for the right one by production
    code either — including it would report an ambiguity nothing could
    actually hit. It also matches the incident this module exists for
    exactly: ``instances_rows`` and ``sac_instances_rows`` were measured
    side by side in the SAME schema (``public``) on the primary, not in two
    different ones.

    ``conn`` is a live ``psycopg`` connection. Each count is read through a
    dynamically quoted, SCHEMA-QUALIFIED identifier built from what
    ``pg_class``/``pg_namespace`` actually report — never by interpolating
    the bare unqualified name a caller might otherwise be tempted to use,
    which is exactly the ambiguity this module exists to remove.
    """
    from psycopg import sql

    names = sorted({f"{prefix}{table}" for prefix in prefixes})
    rows = conn.execute(
        "SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND c.relname = ANY(%s) "
        "AND n.nspname = current_schema() "
        "ORDER BY n.nspname, c.relname",
        (names,),
    ).fetchall()
    out: list[Candidate] = []
    for schema, relname, owner in rows:
        schema, relname, owner = str(schema), str(relname), str(owner)
        count = conn.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(relname)
            )
        ).fetchone()[0]
        out.append(
            Candidate(
                schema=schema,
                table=relname,
                qualified=f"{schema}.{relname}",
                owner=owner,
                count=int(count),
            )
        )
    return out


def find_authoritative(
    conn: Any, candidates: Sequence[Candidate], *, table: str
) -> "Candidate | None":
    """Which candidate an UNQUALIFIED ``SELECT ... FROM <table>`` would hit.

    Matches on ``current_schema()`` — the same resolution Postgres performs
    for an unqualified name via ``search_path``, and the same scope
    ``scitex_dev.store``'s Postgres dialect uses for its own catalog probes
    (``columns_sql`` / ``indexes_sql``: ``... AND table_schema =
    current_schema()``). So this asks the identical question the store's own
    SQL would answer, rather than a related but different one.

    ``None`` means the relation production code would resolve to is not
    among the candidates at all — a harder failure than ambiguity, and the
    caller should treat it as one.
    """
    schema = str(conn.execute("SELECT current_schema()").fetchone()[0])
    for candidate in candidates:
        if candidate["schema"] == schema and candidate["table"] == table:
            return candidate
    return None


def ambiguous(authoritative: Candidate, candidates: Sequence[Candidate]) -> bool:
    """True when reporting only ``authoritative``'s count would mislead.

    Two shapes, both refused:

    * more than one candidate holds rows — which one is the real answer is
      no longer something a bare count can say;
    * ``authoritative`` is EMPTY while a sibling is not — the measured,
      data-loss-looking case in the module docstring, where the honest
      answer is "ambiguous", not "0".
    """
    populated = [c for c in candidates if c["count"] > 0]
    return len(populated) > 1 or (authoritative["count"] == 0 and bool(populated))
