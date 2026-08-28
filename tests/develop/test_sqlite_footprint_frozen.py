"""SQLite must not spread. The footprint is FROZEN and may only shrink.

WHY THIS GATE EXISTS
====================
The operator's instruction, 2026-08-18, when he ranked the fleet's four most
important pieces of work:

    「特に重要なのは移行そのものより、SQLite を新しく作れないようにする監査
      ルールです。これを入れないと半年後にまた SQLite が生えます」

    (What matters is not the migration itself but the audit rule that makes
     new SQLite impossible. Without it, SQLite grows back in six months.)

That is the whole reasoning. A migration moves the databases that exist today;
only a gate stops the next one being written. sac, scitex-cards and scitex-dev
have each grown the same mixed SQLite/PostgreSQL problem independently, which
is the evidence that intent alone does not hold this line.

THE PREDICATE, WRITTEN AS WHAT MUST BE TRUE
===========================================
    Every module under src/ that imports sqlite3 appears in FROZEN_SQLITE.

Stated positively on purpose. "No new SQLite" is unfalsifiable prose; "this
set is a subset of that set" is a thing a machine can check on every push.

TWO DIRECTIONS, AND WHY BOTH ARE TESTED
=======================================
* A module importing sqlite3 that is NOT in the list -> FAIL. This is the
  case the operator asked for: SQLite growing back.
* An entry in the list that NO LONGER imports sqlite3 -> FAIL. This one is
  less obvious and matters more over time. A stale allowlist rots into a set
  of blessed filenames, and a future module reusing a retired name inherits
  permission nobody granted it. Making removal update the list is what keeps
  the list an inventory rather than a wish.

Shrinking is therefore a two-line change (delete the import, delete the
entry) and growing is a conversation. That asymmetry is the point.

WHAT THIS GATE DOES NOT CLAIM
=============================
It does not say the modules listed below are correct, or that they should
stay. The prose deliberately carries NO COUNT: the lists shrink as the
migration lands, and a number written here is a fact no test checks, so it
would go stale silently — which is the exact failure this file exists to
prevent, one level up. Count the sets if you want a number.
They are the measured footprint on 2026-08-19, recorded so the migration has
a definite scope instead of an estimate. Every one of them is per-host state
under ~/.scitex/agent-container/runtime/state.db; the fleet-shared store
(scitex-cards) is already on PostgreSQL at 127.0.0.1:55432.

It also does not police test code or third-party reads. A test using
`:memory:` creates nothing durable, and reading someone else's .db file is
not sac choosing a storage engine.

THE IMPORT CHECK HAD A HOLE, AND THE HOLE IS THE ONE THE OPERATOR ASKED ABOUT
============================================================================
`import sqlite3` is a good proxy for "somebody OPENED a database". It is not
a proxy for "somebody DEFINED a table". Measured 2026-08-19: SIX modules
under src/ carry `CREATE TABLE` DDL and import no sqlite3 at all, because
they hand their DDL to `state_db.open_db` to execute —

    _state/state_db_schema.py            15 CREATE TABLE statements, and it
                                         owns seven of the twelve tables that
                                         currently hold rows
    _state/state_db_incarnations.py      _state/state_db_blocks.py
    _state/state_db_acl_policy.py        _state/state_db_acl_deny_notify.py
    _state/state_db_pending_approval.py

(That enumeration is the original 2026-08-19 measurement, kept as written
because it is what motivated the second list. ``state_db_incarnations.py``
left the set later the same day when the birth certificate moved to
PostgreSQL — the live set is always ``FROZEN_SQLITE_DDL`` below, never this
prose, which is exactly why the gate reads the constant and not the
docstring.)

So the shape "add a new SQLite table without appearing in FROZEN_SQLITE" is
not hypothetical — it is the IDIOMATIC way tables are added in this package,
with five existing examples. A new `state_db_<thing>.py` written that way
grows SQLite back and the gate stays green, which is exactly the six-months-
later outcome the instruction above is meant to prevent.

FROZEN_SQLITE_DDL closes it, with the same two-directional asymmetry: a new
DDL-bearing module fails, and an entry that stops carrying DDL also fails.

WHY TWO SETS RATHER THAN ONE MERGED LIST: they answer different questions and
a single number would answer neither honestly. FROZEN_SQLITE counts modules
that OPEN SQLite; FROZEN_SQLITE_DDL counts modules that DEFINE SQLite tables.
The migration shrinks the second faster than the first, and a reader watching
only the first would conclude SQLite was gone from sac while fifteen table
definitions remained. Keeping them separate is what stops one number being
read as an answer to the other question.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "scitex_agent_container"

#: The measured SQLite footprint on 2026-08-19, as repo-relative paths under
#: ``src/scitex_agent_container/``. THIS LIST MAY ONLY SHRINK.
#:
#: Adding an entry means a new SQLite database in a fleet that is trying to
#: standardise on PostgreSQL — raise it as a decision, not a diff.
FROZEN_SQLITE = frozenset(
    {
        # _authheal/_specimen.py LEFT THIS SET 2026-08-24, and NOT because it
        # was ported — because it should never have been reading storage at
        # all. It opened state.db directly to SELECT from agent_auth_state, a
        # table it does not own. When that table moved to PostgreSQL the read
        # would have returned "<state.db unreadable>" FOREVER rather than
        # failing, since the call sits inside a deliberate fallback that
        # records an unavailable reading instead of aborting the specimen. It
        # now goes through auth_state.get_auth_state(), so the next backend
        # move carries it along instead of stranding it.
        "_lifecycle/_rename_db.py",
        # _lifecycle/_rename_plan.py LEFT THIS SET 2026-08-24, and like
        # _authheal/_specimen.py it left WITHOUT being ported — it was reading
        # a table it does not own. It opened its own sqlite3 connection to
        # SELECT a pid FROM `instances`, which belongs to state_db_instances.
        # That read would have survived the `instances` move to PostgreSQL
        # without raising and simply answered "not running" for every agent,
        # because this caller treats any error as absence of evidence. It now
        # calls list_active_instances(), so the accessor's backend is the only
        # thing that has to change when that table moves.
        #
        # `_rename_db.py` deliberately STAYS: it enumerates sqlite_master to
        # rewrite an agent's rows across every table, which is SQLite-engine
        # code rather than merely SQLite-backed, and is a rewrite rather than
        # a port. It leaves this set when that rewrite happens, not before.
        #
        # _state/auth_state.py LEFT THIS SET 2026-08-24 — the agent auth-verdict
        # cache, moved to per-host PostgreSQL via scitex_dev.store. db_path is
        # gone from every function: it named a file, and there is no file. The
        # accompanying scripts/migrate_auth_state_to_postgres.py carries the
        # existing rows so the cache is not cold on the first `sac agents list`
        # after the switch.
        #
        # THE CONFLICT THAT PRODUCED THIS BLOCK is worth one line, because it
        # will recur: two PRs shrank this set on the same afternoon, each
        # deleting its own entry and keeping the other's. Both deletions were
        # correct and git could not know that. Resolving toward EITHER side
        # alone would have silently re-added a stale entry — which is the
        # precise failure the "no stale entries" test exists to catch, so it
        # would have failed loudly rather than rotted. Expect more of these as
        # the migration lands; take both removals.
        #
        # _state/dispatch_ledger.py LEFT THIS SET 2026-08-28, and its obstacle
        # was not a verb or an endpoint but a QUESTION THE TABLE COULD NOT
        # ANSWER: whose dispatch is this. state.db was PER-AGENT, so the shard
        # did the scoping and no column had to; SCITEX_STORE_DSN is FLEET-WIDE,
        # so a naive port collapsed 130+ shards into one table where
        # list_dispatches() answered for the whole fleet and
        # list_unreacted_dispatches() reported everyone else's comm-misses as
        # yours. Nothing would have raised. from_agent could not stand in for
        # the owner — it names the SENDER of one message and is explicitly
        # nullable — so the port added `agent` to the store IDENTITY, the shape
        # inbound_ledger had already needed one table earlier. Its four readers
        # (_network/_peer_dispatch, _mcp/_channel_tools, _mcp/
        # _channel_reaction_ack and _mcp/channel) moved in the SAME PR: a
        # half-moved table is a split brain that also raises nothing.
        # _state/inbound_ledger.py LEFT THIS SET 2026-08-20 — the FOURTH table
        # to move to PostgreSQL. Its obstacle was neither a missing verb nor a
        # missing endpoint but an AUTOINCREMENT id that looked public: returned
        # by record_inbound, taken by mark_reported. Measured before assuming —
        # no caller in this repo binds that return value, and mark_reported is
        # always handed a claim-derived id — so the integer only ever
        # round-tripped claim -> settle inside one Stop hook. The natural key
        # replaced it with no surrogate, which matters because surrogate ids do
        # not survive a store boundary and this fleet has already paid for that
        # once. The atomic BEGIN IMMEDIATE claim became an optimistic loop on
        # Row.seq, preserving "two concurrent Stop hooks never double-report".
        "_state/port_allocator.py",
        "_state/state_db.py",
        "_state/state_db_health.py",
        "_state/state_db_heartbeats.py",
        "_state/state_db_migrations.py",
        "_state/state_db_relocation.py",
        # _state/state_db_verdict_dedup.py LEFT THIS SET 2026-08-19 — the
        # first table to move to PostgreSQL, by adopting
        # scitex_dev.store rather than by sac growing its own psycopg
        # layer. The ratchet is the point: this file FAILS if a module
        # is listed here but no longer imports sqlite3, so a port that
        # forgets to shrink the set is caught, not merely uncelebrated.
    }
)

# `import sqlite3` / `import sqlite3 as x` / `from sqlite3 import ...`, at any
# indentation (several of these imports are function-local, deliberately).
_IMPORTS_SQLITE = re.compile(
    r"^\s*(?:import\s+sqlite3\b|from\s+sqlite3\s+import\b)", re.M
)


#: Modules that DEFINE SQLite tables without opening a connection themselves —
#: they hand their DDL to ``state_db.open_db``. Invisible to FROZEN_SQLITE by
#: construction; see the module docstring. THIS LIST MAY ONLY SHRINK.
FROZEN_SQLITE_DDL = frozenset(
    {
        # _state/state_db_acl_deny_notify.py LEFT THIS SET 2026-08-20 — the
        # FIFTH table, moved in the same PR as comms_blocks because the two are
        # siblings that both lose a line from one block of init_schema.
        #
        # It is also the first table whose move had a SECOND SITE: it was in
        # KNOWN_TABLES, so state_db_export would have queried a table SQLite no
        # longer has. comms_blocks was not, which is exactly why the pair had
        # to be checked separately rather than assumed symmetric.
        #
        # And this list's OWN staleness gate is what caught the omission: the
        # DDL left the module in one commit and the entry stayed here, which
        # `test_the_ddl_freeze_list_has_no_stale_entries` reported. A ratchet
        # that only checks one direction would have gone green on a half-done
        # migration.
        "_state/state_db_acl_policy.py",
        # _state/state_db_blocks.py LEFT THIS SET 2026-08-20 — the FOURTH table
        # to move to PostgreSQL, and the first where the migration's own
        # PREDICTION was wrong in the safe direction. It was scoped as "a
        # DURABLE decision, not a transient flag, so it almost certainly holds
        # real rows and DOES need a migration". It holds zero: 52 SQLite
        # databases read across compute-01..04 (the fleet state.db plus every
        # per-agent shard), zero rows in all of them. Nobody has ever blocked
        # anyone. Checking beat carrying the previous slice's conclusion over.
        # _state/state_db_pending_approval.py LEFT THIS SET 2026-08-20 — the
        # THIRD table to move to PostgreSQL, and the first whose SQLite verb
        # had no store equivalent: it DELETEd, and the store only hides. That
        # turned out to be the better primitive (the decision stays in the
        # oplog with its actor) but it introduced a lifecycle the SQLite
        # version did not have, and the module documents it.
        "_state/state_db_schema.py",
        # _state/state_db_incarnations.py LEFT THIS SET 2026-08-19 — the
        # SECOND table to move to PostgreSQL, and the first to leave via
        # THIS list rather than FROZEN_SQLITE (it never imported sqlite3;
        # it handed its DDL to state_db.open_db, which is exactly the hole
        # this second list was added to close). The birth certificate now
        # lives in per-host PostgreSQL via scitex_dev.store.
    }
)

# `CREATE TABLE` / `CREATE TABLE IF NOT EXISTS`, any case, any indentation.
_DEFINES_A_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.I)


def _modules_defining_tables() -> set[str]:
    """Every module under src/ carrying CREATE TABLE DDL, as relative paths.

    Deliberately NOT excluding modules that also import sqlite3 — a module can
    legitimately be in both sets, and subtracting one from the other would
    make each list depend on the other's accuracy.
    """
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if _DEFINES_A_TABLE.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(path.relative_to(SRC).as_posix())
    return found


def _modules_importing_sqlite() -> set[str]:
    """Every module under src/ that imports sqlite3, as relative paths."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if _IMPORTS_SQLITE.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(path.relative_to(SRC).as_posix())
    return found


def test_the_scan_actually_finds_something() -> None:
    """POSITIVE CONTROL — a zero here would make both gates below vacuous.

    An empty result and a healthy codebase produce the same PASS in the two
    tests that follow, so without this the whole file could go green because
    the glob broke rather than because the rule holds.
    """
    # Arrange
    scanned_root = SRC
    # Act
    found = _modules_importing_sqlite()
    # Assert
    assert found, (
        f"scanned {scanned_root} for sqlite3 imports and found NONE. Either every "
        "SQLite user was removed (delete this file and FROZEN_SQLITE with it) "
        "or the scan is broken. Do not assume the former."
    )


def test_no_module_outside_the_frozen_list_imports_sqlite() -> None:
    """The operator's rule: SQLite must not grow back."""
    # Arrange
    found = _modules_importing_sqlite()
    # Act
    new = sorted(found - FROZEN_SQLITE)
    # Assert
    assert not new, (
        "NEW SQLITE. These modules import sqlite3 and are not in the frozen "
        f"footprint: {new}. The fleet is standardising on PostgreSQL "
        "(scitex-cards already runs on 127.0.0.1:55432). If this module "
        "genuinely needs local per-host state, that is a decision to raise "
        "rather than a line to add to FROZEN_SQLITE."
    )


def test_the_frozen_list_has_no_stale_entries() -> None:
    """The list must stay an inventory, not a set of blessed filenames.

    Without this, removing a SQLite user leaves its path permitted forever,
    and a later module reusing that path inherits permission silently.
    """
    # Arrange
    found = _modules_importing_sqlite()
    # Act
    stale = sorted(FROZEN_SQLITE - found)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE lists modules that no longer import sqlite3: "
        f"{stale}. Good news — the footprint shrank. Delete these entries so "
        "the list keeps describing reality; a stale allowlist re-opens the "
        "door it was written to close."
    )


# ----------------------------------------------------------------------
# The DDL footprint — the hole the import check could not see.
# ----------------------------------------------------------------------


def test_the_ddl_scan_actually_finds_something() -> None:
    """POSITIVE CONTROL — a zero here would make both DDL gates vacuous.

    Same reasoning as the import scan's control: an empty result and a
    SQLite-free codebase produce the same PASS below, so without this the
    whole section could go green because the glob broke.
    """
    # Arrange
    scanned_root = SRC
    # Act
    found = _modules_defining_tables()
    # Assert
    assert found, f"no CREATE TABLE found anywhere under {scanned_root}"


def test_no_module_defines_a_new_sqlite_table() -> None:
    """A module may not DEFINE a SQLite table without being frozen.

    This is the case `import sqlite3` cannot see, and it is the idiomatic way
    tables are added here — five existing modules do exactly this. Without
    this gate, `state_db_<newthing>.py` grows SQLite back on a green build.
    """
    # Arrange
    frozen = FROZEN_SQLITE_DDL | FROZEN_SQLITE
    # Act
    new = sorted(_modules_defining_tables() - frozen)
    # Assert
    assert not new, (
        "these modules define SQLite tables and are not frozen: "
        f"{new}. sac is migrating state to PostgreSQL via scitex_dev.store; "
        "a new SQLite table is a decision to raise, not a line to add to "
        "FROZEN_SQLITE_DDL."
    )


def test_the_ddl_freeze_list_has_no_stale_entries() -> None:
    """An entry that no longer defines a table must leave the list.

    Same asymmetry as the import list: shrinking is two lines, and a stale
    allowlist rots into blessed filenames that a future module inherits.
    """
    # Arrange
    defining = _modules_defining_tables()
    # Act
    stale = sorted(FROZEN_SQLITE_DDL - defining)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE_DDL lists modules that no longer define a table: "
        f"{stale}. Delete the entries — the list is an inventory, not a "
        "set of blessed filenames."
    )
