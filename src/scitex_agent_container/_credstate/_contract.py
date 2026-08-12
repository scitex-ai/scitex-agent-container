"""The ADR-0022 §5.1 sync-column contract, as executable code.

ADR-0022 §5.1 states the rule and §7.3 admits it is unenforced::

    "A table created without these can never be synchronised without a
     rewrite. That is the one sentence every consumer stream needs
     tonight, because tables are being created right now."

    §7.3 — "the §5.1 schema contract is not enforced anywhere (no
     linter, no base-table helper)."

This module is that missing base-table helper. It is deliberately generic
— it knows nothing about credentials — because the contract it enforces
governs *every* table any consumer stream creates, and a contract that
lives inside one domain's module protects only that domain.

Two surfaces:

* :func:`sync_columns_sql` — emit the five columns, so a new table gets
  them by construction rather than by the author remembering.
* :func:`assert_sync_contract` — parse a ``CREATE TABLE`` and REFUSE it
  if the columns are absent, nullable where they must not be, or typed
  wrongly. This is what turns "should" into "cannot".

The parser is deliberately small and textual. It is not a SQL engine and
does not try to be: it reads the column-definition list of a single
``CREATE TABLE`` statement, which is the only construct the contract
constrains. A shape it cannot parse raises rather than passing — an
unenforceable contract must fail closed, since the whole point is that a
table which slips through can never be synchronised without a rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


class SyncContractError(ValueError):
    """A table definition violates the ADR-0022 §5.1 sync-column contract.

    Always names the table and the offending column. Raised at import or
    schema-assert time, never at row-write time — the point is that a
    non-conforming table cannot come into existence.
    """


@dataclass(frozen=True)
class SyncColumn:
    """One of the five mandatory columns, with the reason it exists.

    ``reason`` is carried because the error message is the whole product
    here: an author who trips this needs to know why the column is not
    negotiable, not merely that it is missing.
    """

    name: str
    sql_type: str
    not_null: bool
    reason: str

    def ddl(self) -> str:
        null = "NOT NULL" if self.not_null else "NULL"
        return f"{self.name} {self.sql_type} {null}"


#: The five columns of ADR-0022 §5.1, verbatim in intent.
#:
#: ``updated_at`` is NOT NULL but is explicitly NOT the conflict rule —
#: the ADR says "reporting and ordering for humans — never the conflict
#: rule", and :mod:`._schema` records the actual rule per table.
SYNC_COLUMNS: tuple[SyncColumn, ...] = (
    SyncColumn(
        "row_uuid",
        "UUID",
        True,
        "globally unique row identity, minted at insert — without it a "
        "row pulled from a peer cannot be told apart from a local one",
    ),
    SyncColumn(
        "origin_node",
        "TEXT",
        True,
        "which node authored the row; the ownership partition key that "
        "makes single-writer conflict resolution possible at all",
    ),
    SyncColumn(
        "revision",
        "BIGINT",
        True,
        "monotonic, bumped by the owner on every update — the ONLY "
        "sanctioned arbiter of newer-ness (wall clocks skew across hosts)",
    ),
    SyncColumn(
        "updated_at",
        "TIMESTAMPTZ",
        True,
        "reporting and ordering for humans; ADR-0022 §5.2 PROHIBITS "
        "using it as the conflict rule",
    ),
    SyncColumn(
        "deleted_at",
        "TIMESTAMPTZ",
        False,
        "tombstone; rows are never DELETEd, so a delete can replicate",
    ),
)

SYNC_COLUMN_NAMES: frozenset[str] = frozenset(c.name for c in SYNC_COLUMNS)


#: The four conflict classes of ADR-0022 §5.2, with the rule each implies.
#:
#: A table declares its class; the class decides what an incoming remote
#: row may do. ``shared_mutable`` is the conservative default for
#: anything more than one node may edit.
CONFLICT_CLASSES: Mapping[str, str] = {
    "configuration": (
        "NOT SYNCED. Git is the sync. Rows are generated from files under "
        "version control; a database copy is a cache, never an authority."
    ),
    "log": (
        "UNION, conflict impossible by construction. The PRIMARY KEY must "
        "include origin_node, so two nodes can never author the same key; "
        "ON CONFLICT DO NOTHING is therefore correct — a collision means "
        "the identical row arrived twice."
    ),
    "state": (
        "SINGLE-WRITER, partitioned by origin_node. A node may UPDATE only "
        "rows where origin_node = its own node_id; every other node's rows "
        "are read-only replicas. Conflict is impossible by construction, "
        "not by arbitration."
    ),
    "shared_mutable": (
        "NO AUTOMATIC MERGE. Accept a remote row only when "
        "remote.origin_node == local.origin_node AND remote.revision > "
        "local.revision. In every other case DO NOTHING and write a "
        "divergence record for operator adjudication."
    ),
}

#: Classes whose rows travel between hosts and therefore need the columns.
SYNCED_CLASSES: frozenset[str] = frozenset(
    {"log", "state", "shared_mutable"}
)


def sync_columns_sql(*, indent: str = "    ") -> str:
    """Emit the five mandatory column definitions, comment included.

    Used by :mod:`._schema` so a new table acquires the contract by
    construction. Hand-writing them is what ADR-0022 §7.3 identifies as
    the gap; the helper exists so that nobody has to.
    """
    lines = [f"{indent}-- ADR-0022 §5.1 sync contract (see _credstate._contract)"]
    lines += [f"{indent}{col.ddl()}," for col in SYNC_COLUMNS]
    return "\n".join(lines)


_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.\"]*)\s*\(",
    re.IGNORECASE,
)


_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def strip_comments(ddl: str) -> str:
    """Remove ``--`` line comments before any structural parsing.

    Not cosmetic. The generated column block carries an explanatory
    comment, and comment text routinely contains parentheses and commas —
    both of which are the exact tokens the depth counter and the
    top-level split rely on. Parsing before stripping silently mis-reads
    the table, which for a fail-closed contract means rejecting a
    conforming table.
    """
    return _LINE_COMMENT_RE.sub("", ddl)


def _body_of(ddl: str) -> tuple[str, str]:
    """Return ``(table_name, column-definition body)`` for one CREATE TABLE.

    Depth-counts parentheses so that types like ``NUMERIC(10,2)`` and
    inline ``CHECK (...)`` constraints do not truncate the body early.
    """
    ddl = strip_comments(ddl)
    match = _CREATE_RE.search(ddl)
    if match is None:
        raise SyncContractError(
            "not a parseable CREATE TABLE statement, so its compliance with "
            "the ADR-0022 §5.1 sync contract cannot be established. The "
            "contract fails CLOSED: a table that cannot be checked is "
            "treated as non-conforming, because one that slips through can "
            "never be synchronised without a rewrite."
        )
    start = match.end()
    depth = 1
    for index in range(start, len(ddl)):
        char = ddl[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return match.group("name"), ddl[start:index]
    raise SyncContractError(
        f"unbalanced parentheses in the definition of table "
        f"{match.group('name')!r}; cannot verify the §5.1 sync contract."
    )


def _split_top_level(body: str) -> list[str]:
    """Split a column-definition body on top-level commas only."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def parse_columns(ddl: str) -> dict[str, str]:
    """Map column name -> its full definition, for one CREATE TABLE."""
    _name, body = _body_of(ddl)
    columns: dict[str, str] = {}
    for part in _split_top_level(body):
        head = part.split(None, 1)[0].strip('"')
        if head.upper() in {
            "PRIMARY",
            "UNIQUE",
            "FOREIGN",
            "CHECK",
            "CONSTRAINT",
            "EXCLUDE",
        }:
            continue
        columns[head] = part
    return columns


def table_name_of(ddl: str) -> str:
    """The table name declared by one ``CREATE TABLE`` statement."""
    return _body_of(ddl)[0].strip('"')


def assert_sync_contract(ddl: str, *, conflict_class: str) -> None:
    """Raise unless ``ddl`` satisfies ADR-0022 §5.1 for its class.

    ``configuration`` tables are exempt: §5.2 rule 1 removes them from
    sync entirely (git is their sync), so mandating replication columns
    on them would be cargo cult. Every other class is checked.

    For ``log`` the PRIMARY KEY is additionally required to include
    ``origin_node`` — that is precisely what makes "conflict impossible
    by construction" true rather than aspirational, and a log table
    without it silently degrades to last-write-wins.
    """
    if conflict_class not in CONFLICT_CLASSES:
        raise SyncContractError(
            f"unknown conflict class {conflict_class!r}; ADR-0022 §5.2 "
            f"defines exactly {sorted(CONFLICT_CLASSES)}. A table must "
            "declare how its rows merge before it may hold any."
        )
    if conflict_class not in SYNCED_CLASSES:
        return

    name = table_name_of(ddl)
    columns = parse_columns(ddl)
    for col in SYNC_COLUMNS:
        definition = columns.get(col.name)
        if definition is None:
            raise SyncContractError(
                f"table {name!r} is missing the mandatory sync column "
                f"{col.name!r} ({col.reason}). ADR-0022 §5.1: a table "
                "created without these can never be synchronised without a "
                "rewrite."
            )
        upper = definition.upper()
        if col.sql_type.upper() not in upper:
            raise SyncContractError(
                f"table {name!r} column {col.name!r} must be typed "
                f"{col.sql_type}; got: {definition.strip()}"
            )
        declared_not_null = "NOT NULL" in upper
        if col.not_null and not declared_not_null:
            raise SyncContractError(
                f"table {name!r} column {col.name!r} must be NOT NULL "
                f"({col.reason}). A nullable {col.name} is a row that can "
                "arrive with no answer to the question the column exists "
                "to answer."
            )
        if not col.not_null and declared_not_null:
            raise SyncContractError(
                f"table {name!r} column {col.name!r} must stay nullable — "
                "it is the tombstone, and NULL is what 'not deleted' means."
            )

    if conflict_class == "log":
        _assert_origin_in_primary_key(ddl, name=name)


_PK_INLINE_RE = re.compile(r"PRIMARY\s+KEY\s*\(([^)]*)\)", re.IGNORECASE)


def _assert_origin_in_primary_key(ddl: str, *, name: str) -> None:
    _table, body = _body_of(ddl)
    match = _PK_INLINE_RE.search(body)
    if match is None:
        raise SyncContractError(
            f"log-class table {name!r} declares no composite PRIMARY KEY. "
            "ADR-0022 §5.2 rule 2 makes append-only union safe by putting "
            "origin_node in the key so two nodes cannot author the same "
            "row; without it ON CONFLICT DO NOTHING silently drops a "
            "peer's distinct row."
        )
    key_columns = {c.strip().strip('"') for c in match.group(1).split(",")}
    if "origin_node" not in key_columns:
        raise SyncContractError(
            f"log-class table {name!r} has PRIMARY KEY "
            f"({', '.join(sorted(key_columns))}) which omits 'origin_node'. "
            "ADR-0022 §5.2 rule 2 requires it: a collision must mean the "
            "identical row arrived twice, never that two nodes disagreed."
        )
