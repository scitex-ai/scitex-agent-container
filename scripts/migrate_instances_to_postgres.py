#!/usr/bin/env python3
"""One-shot: copy the SQLite ``instances`` table into PostgreSQL.

Companion to the ``state_db_instances`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so a host's whole agent
lifecycle history is not stranded in a file nothing opens any more.

EVERY ROW MOVES, ENDED ONES INCLUDED
====================================
It is tempting to carry only the live rows — they are the ones
``list_active_instances`` returns and the ones an operator sees. That would
be wrong, and three readers say so:

* ``last_known_instance`` exists PRECISELY to answer from an ended row: the
  #192 fail-loud message names the last known placement of an agent that is
  not running now.
* ``cli_pkg/lifecycle/_restart_verify`` reads an ended row for the tmux
  session name it pairs with ``#{session_created}``. Without the ended row
  it abstains on every restart it is asked to verify.
* ``_reconcile/_rule`` reads ``exit_reason`` off an ended row to decide
  whether a managed agent should be restarted. A missing row is
  ``NEVER_STARTED``, which is a DIFFERENT verdict — so dropping history
  would not merely lose detail, it would change decisions.

So the source query is unfiltered and ``ended_at`` / ``exit_reason`` /
``started_at`` / ``last_heartbeat_at`` are carried VERBATIM. Never
re-stamped: ``started_at`` and ``ended_at`` are IMMUTABLE in the store, and
stamping ``now()`` would both erase the evidence and make the first real
contradiction unreportable.

WHAT DOES NOT MOVE, AND WHY EACH IS A REAL (ACCEPTED) LOSS
==========================================================
``definition_id`` / ``scope`` / ``ppid``
    Dropped from the schema. No reader in ``src/`` ever touched them and no
    writer ever set the first or the last; ``scope`` was the literal
    ``'global'`` on every row. See ``_store_plugin.INSTANCES``.
``bound_port``
    FOLDED, not dropped: ``COALESCE(a2a_port, bound_port)`` becomes the
    store's single ``a2a_port``. Both columns always carried one value
    written twice, and the split is what let two routing readers give
    different answers about the same row. The production reader mirrors the
    value back out under both KEYS, so no caller changes shape.

IDENTITY IS ``{id, host}``, VERBATIM, AND CROSS-HOST DUPLICATES ARE REAL
========================================================================
``id`` is a uuid7 minted at the origin, so two hosts cannot collide. What
looks like a duplicate is not one: when the lead dispatches an agent to a
peer it writes its OWN row with ``remote=1`` and the peer's hostname, while
the peer writes its own local row for the same agent. Those are two
observations by two hosts and PER_HOST truth keeps them as two records —
which is exactly why ``host`` is in the identity. This script must not
"deduplicate" them and does not try.

THAT IS ALSO WHY THERE IS NO COLLISION CHECK
--------------------------------------------
``migrate_comms_nodes_to_postgres`` refuses to pick a winner when two hosts
disagree about one NAME, because that table's identity is the name and the
files were allowed to diverge. Here the identity already carries the host,
so two hosts' rows are different records and there is nothing to disagree
about. A re-run finds its own rows already present and leaves them alone;
that is the ordinary path, not a conflict.

VERIFICATION ASKS ABOUT THIS HOST, NOT ABOUT THE STORE
=======================================================
``_migrate_lib``'s default verify compares the source row count against the
store's TOTAL, which cannot hold for a SHARED store: once compute-04 has
migrated, spartan's run reads (say) 200 local rows and sees 800 in the
store. That comparison would pass by accident here and would fail for the
FIRST host if any peer had gone first with fewer rows — a check whose
verdict depends on run order is not a check.

So :func:`_verify` counts the records this host's file was responsible for:
it re-reads the source ids through the PRODUCTION reader and reports how
many are visible. Reading back through the production path — rather than
trusting the write count — is the part of the library's discipline that
matters most and is kept: a put that silently no-opped would otherwise
report as success.

Nothing is deleted from SQLite; the old table stays as a fallback.

A DRY RUN IS THE DEFAULT and it must be RUN ON THE HOST — ``_migrate_lib``
owns both rules and the ``$HOME`` trap behind the second one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# ...and this script's OWN directory, which a direct ``python scripts/x.py``
# supplies for free and an ``importlib.util.spec_from_file_location`` load
# does not. The develop-tier test that pins "a bare invocation writes
# nothing" loads these scripts the second way, so without this line the
# guard cannot even import the thing it is guarding.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _migrate_lib import SqliteSource, run_migration  # noqa: E402

from scitex_agent_container._state.state_db_instances import (  # noqa: E402
    scan_instances,
)
from scitex_agent_container._state.state_db_instances_store import (  # noqa: E402
    ACTOR,
    INSTANCES_STORE,
    new_instances_store,
    run_with_reconnect,
)

#: The role that must OWN sac's store tables. Every agent connects as
#: ``ywatanabe__<agent>``; only the tables' owner may ALTER or DROP them, and
#: ``scitex_store_owner`` is the identity the fleet's other stores are owned
#: by.
#:
#: ``SAC_MIGRATE_OWNER_ROLE`` overrides the name, and setting it to the EMPTY
#: string opts out entirely — for a single-role deployment where no such role
#: exists and every reader is the writer. Opting out is loud in the output,
#: because the incident this guard exists for looked exactly like a healthy
#: run until the fleet started failing.
DEFAULT_STORE_OWNER_ROLE = "scitex_store_owner"


def _owner_role() -> str:
    """The configured owner role, read AT CALL TIME.

    Deliberately not a module-level constant. A constant is evaluated at
    IMPORT, so anything that imports this script before setting the variable
    — a test harness, a wrapper, an operator's REPL — would silently get the
    default and the override would look like it had done nothing. Measured
    while writing the smoke test for this very guard: the opt-out was set
    after the import and the refusal fired anyway.
    """
    return os.environ.get("SAC_MIGRATE_OWNER_ROLE", DEFAULT_STORE_OWNER_ROLE)


def _with_role(dsn: str, role: str) -> str:
    """Return ``dsn`` with libpq ``options=-c role=<role>``.

    The role has to travel IN the DSN because ``scitex_dev``'s Postgres
    dialect calls ``psycopg.connect(target.dsn)`` with nothing else. An
    ``options`` already present is left alone — the operator who wrote it
    outranks this default.
    """
    if "options=" in dsn:
        return dsn
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-c%20role%3D{role}"


def _preflight_ownership(log) -> "str | None":
    """REFUSE unless the store's tables will be owned by the right role.

    MEASURED 2026-08-28, on the channel_events migration, by the operator:
    the migration CREATES the physical tables, so they come out owned by
    whichever role ran it. That run used ``ywatanabe__cli``; every agent
    connects as ``ywatanabe__<agent>``, so three minutes later the fleet's
    message channel was failing with ``InsufficientPrivilege: must be owner
    of table sac_channel_events`` and stayed broken for about six minutes.

    So this refuses BEFORE writing rather than letting the fault surface as
    an outage. It returns the DSN to use (with ``role=`` pinned) or ``None``
    when the run must abort. Three cases, and they are deliberately not
    collapsed:

    * already ``scitex_store_owner`` — proceed unchanged.
    * a MEMBER of it — pin ``role=`` so every CREATE lands owned correctly.
    * neither — REFUSE, naming the grant the operator needs.

    A store whose tables ALREADY exist is not exempt: this migration adds
    rows to tables the first ``Store`` open may still have to create (the
    oplog and cursor tables are created lazily), so the wrong role can still
    leave a fresh table behind.
    """
    role = _owner_role()
    if not role:
        log(
            "  ownership preflight OPTED OUT (SAC_MIGRATE_OWNER_ROLE=''). "
            "The tables this run creates will be owned by the connecting "
            "role. That is correct ONLY where every reader connects as that "
            "same role; on this fleet every agent connects as "
            "ywatanabe__<agent> and the mismatch is a 6-minute outage."
        )
        return os.environ.get("SCITEX_STORE_DSN", "")
    dsn = os.environ.get("SCITEX_STORE_DSN", "")
    if not dsn:
        log(
            "  ownership preflight SKIPPED — SCITEX_STORE_DSN is unset, so "
            "the store resolves to this host's PostgreSQL and this check "
            "cannot pin a role in the DSN. Run with SCITEX_STORE_DSN set."
        )
        return dsn or None
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the Postgres path needs psycopg
        log("  ownership preflight SKIPPED — psycopg is not installed")
        return dsn
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            # Ask whether the role EXISTS first. ``pg_has_role`` RAISES on an
            # unknown role, and "the owner role is not provisioned here" is a
            # different fact with a different remedy from "you are not a
            # member of it" — collapsing them into one connection error is
            # how an operator ends up reading a CREATE ROLE problem as a
            # GRANT problem.
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            if not exists:
                log(
                    f"  REFUSING: role {role!r} does not exist on "
                    f"this server, so the tables this run creates cannot be "
                    f"owned by it. Fix: provision the role, or re-run with "
                    f"SAC_MIGRATE_OWNER_ROLE naming the role that should own "
                    f"them (or ='' to opt out on a single-role deployment)."
                )
                return None
            row = conn.execute(
                "SELECT current_user, "
                "pg_has_role(current_user, %s, 'MEMBER')",
                (role,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log(f"  ownership preflight FAILED to connect: {exc!r}")
        return None
    current, is_member = str(row[0]), bool(row[1])
    if current == role:
        log(f"  ownership: connected as {current} — tables land owned correctly")
        return dsn
    if is_member:
        log(
            f"  ownership: {current} is a member of {role}; "
            f"pinning role={role} so every CREATE is owned by it"
        )
        return _with_role(dsn, role)
    log(
        f"  REFUSING: connected as {current!r}, which is NOT {role!r} "
        f"and not a member of it. This migration CREATES the store's physical "
        f"tables, so they would be owned by {current!r} and every agent "
        f"(which connects as ywatanabe__<agent>) would fail with "
        f"InsufficientPrivilege. Measured on the channel_events migration on "
        f"2026-08-28: the fleet's message channel broke three minutes after "
        f"the run and stayed broken ~6 minutes.\n"
        f"  Fix: run as {role}, or "
        f"`GRANT {role} TO {current}` and re-run."
    )
    return None


def _verify_as_consumer(role: str, log) -> bool:
    """Read the migrated rows back as ``role``. False when it cannot.

    MEASURED 2026-08-28, same incident: the post-migration check passed while
    the fleet was broken, because it ran as the MIGRATING role — the one
    identity guaranteed to work. A verification that uses the writer's
    credential proves the writer can read its own writes and nothing else.
    """
    dsn = os.environ.get("SCITEX_STORE_DSN", "")
    if not dsn:
        log("  consumer verify SKIPPED — SCITEX_STORE_DSN is unset")
        return True
    import psycopg

    try:
        with psycopg.connect(_with_role(dsn, role), connect_timeout=5) as conn:
            n = conn.execute("SELECT COUNT(*) FROM instances_rows").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log(
            f"  CONSUMER VERIFY FAILED as {role!r}: {exc!r}\n"
            f"  The rows are written but the fleet cannot READ them. This is "
            f"the outage shape the ownership preflight exists to prevent."
        )
        return False
    log(f"  consumer verify: {role} can read {n} record(s)")
    return True

SOURCE = SqliteSource(
    table="instances",
    columns=(
        "id",
        "name",
        "host",
        "pid",
        "screen",
        "workdir",
        "a2a_port",
        "bound_port",
        "remote",
        "spawned_by",
        "started_at",
        "last_heartbeat_at",
        "iter_count",
        "input_tokens",
        "output_tokens",
        "ended_at",
        "exit_reason",
    ),
    # ``started_at`` rather than rowid: it is the order an operator reads a
    # lifecycle table in, and it makes the dry-run listing stable across a
    # re-run even if rows were rewritten in place. The ``id`` tiebreak is the
    # one the production readers use.
    order_by="started_at ASC, id ASC",
)

#: Source ids seen by the last :func:`_record` pass, so :func:`_verify` can
#: ask about THIS host's rows rather than about the whole shared store. A
#: module global rather than a parameter because ``run_migration`` owns the
#: call sequence and hands ``verify`` nothing.
_SEEN_IDS: list[str] = []


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One SQLite row as the store's record.

    ``None`` values are DROPPED rather than written. That is not tidiness:
    the store freezes an IMMUTABLE field at its first stamped value, and
    writing ``None`` counts as a stamp — so a migrated live row that carried
    ``ended_at=None`` explicitly could never be ended again, and nothing
    would raise (the rejection comes back in ``PutResult.conflicts``, which
    no caller reads).
    """
    identifier = str(row["id"])
    if identifier not in _SEEN_IDS:
        _SEEN_IDS.append(identifier)
    port = row.get("a2a_port")
    if port is None:
        port = row.get("bound_port")
    values: dict[str, Any] = {
        "id": identifier,
        "host": str(row["host"]),
        "name": row.get("name"),
        "pid": row.get("pid"),
        "a2a_port": port,
        "screen": row.get("screen"),
        "workdir": row.get("workdir"),
        # ``remote`` is always written, including False. It is the
        # authoritative locality flag the GC and five readers branch on, and
        # SQLite's DEFAULT 0 means an absent value there meant "local" —
        # carrying that as an unwritten field would turn a known False into
        # an unknown.
        "remote": bool(row.get("remote")),
        "spawned_by": row.get("spawned_by"),
        "started_at": row.get("started_at"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "iter_count": row.get("iter_count"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "ended_at": row.get("ended_at"),
        "exit_reason": row.get("exit_reason"),
    }
    return {
        key: value
        for key, value in values.items()
        if value is not None or key == "remote"
    }


def _key(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "host": record["host"]}


def _describe(row: Mapping[str, Any]) -> str:
    state = "ENDED" if row.get("ended_at") else "live "
    port = row.get("a2a_port") or row.get("bound_port") or "-"
    return (
        f"{state} {row.get('started_at')}  {row.get('name')}@{row.get('host')}"
        f":{port}  id={row.get('id')}"
    )


def _verify() -> int:
    """How many of THIS host's source rows the production reader can see.

    Deliberately NOT ``len(all records in the store)`` — see the module
    docstring for why a shared store makes that comparison order-dependent.

    ``scan_instances`` on the shared handle is the SAME read
    ``list_active_instances`` and ``last_known_instance`` perform (they add a
    filter to it and nothing else), so a record the store accepted but cannot
    serve counts as absent here — which is the whole reason verification
    reads back rather than trusting the write count. One scan rather than one
    point-read per row: a point-read is itself a scan (the identity is
    ``{id, host}`` and only the id is known), so N of them would be N² on a
    table whose whole point is that it is the biggest one.
    """
    present = {
        str(row.values.get("id")) for row in run_with_reconnect(scan_instances)
    }
    return sum(1 for identifier in _SEEN_IDS if identifier in present)


def main(argv: list[str] | None = None) -> int:
    """Preflight ownership, migrate, then verify with a CONSUMER credential.

    The two guards around ``run_migration`` are not decoration; each is a
    measured incident from the sibling migrations run on 2026-08-28. See
    :func:`_preflight_ownership` and :func:`_verify_as_consumer`.

    NO CROSS-HOST ORDERING ASSUMPTION IS MADE, and that is worth stating
    because the channel_events migration's ordering guard turned out to be
    structurally unsatisfiable on this fleet (145 rows that looked like
    post-cutover writes were another host's legitimately imported history,
    because two hosts served the SAME targets over OVERLAPPING periods).
    ``instances`` is immune BY CONSTRUCTION rather than by luck: the record
    identity is ``{id, host}`` with a uuid7 minted at the origin, so two
    hosts' rows are different records that never compare, and this script
    has no rule that reads one host's timestamps against another's. An
    interleaved history is simply two sets of records.
    """
    committing = "--commit" in (argv if argv is not None else sys.argv[1:])
    if committing:
        pinned = _preflight_ownership(print)
        if pinned is None:
            return 1
        os.environ["SCITEX_STORE_DSN"] = pinned

    rc = run_migration(
        argv=argv,
        description=__doc__,
        source=SOURCE,
        store_name=INSTANCES_STORE,
        # ``new_...``, not the shared ``open_...``: the library closes the
        # handle it is given, and closing the process-wide one would leave
        # the verification read below holding a dead connection.
        open_store=new_instances_store,
        to_record=_record,
        key_of=_key,
        verify=_verify,
        describe_row=_describe,
        # No ``should_hide``: an ``ended_at`` row is a TOMBSTONE, not a
        # withdrawal. ``last_known_instance`` must keep returning it (see the
        # module docstring), and ``hide()`` would make it read as absent to
        # every production reader — which is the one answer three of them
        # must never be handed by mistake.
        actor=ACTOR,
    )
    if rc == 0 and committing:
        role = os.environ.get("SAC_MIGRATE_VERIFY_ROLE", "")
        if role:
            if not _verify_as_consumer(role, print):
                return 1
        else:
            print(
                "  WARNING: the verification above ran as the MIGRATING role, "
                "which is the one identity guaranteed to work. Set "
                "SAC_MIGRATE_VERIFY_ROLE=<an agent's role> to re-read as a "
                "CONSUMER. Measured 2026-08-28: a writer-credential check "
                "passed while the fleet could not read the rows at all."
            )
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
