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

src/ WAS NEVER THE WHOLE FOOTPRINT
==================================
Measured 2026-08-29: ten files under ``scripts/`` and eleven under ``tests/``
carry an ``import sqlite3``, and none of them were visible to any gate here,
because every scan hard-coded ``SRC.rglob``. That is not a rounding error
next to the handful left under ``src/`` — it is the bulk of what remains,
and a migration carrier is exactly the kind of file that gets copied into a
new one. ``FROZEN_SQLITE_SCRIPTS`` and ``FROZEN_SQLITE_TESTS`` freeze those
two populations under the same two-directional rule as ``FROZEN_SQLITE``.

The docstring above says this gate "does not police test code". That was true
of the ORIGINAL predicate and is now narrower than it reads: what the gate
still does not do is forbid a test from touching SQLite — a ``:memory:``
fixture creates nothing durable. What it does now is COUNT them, so the set
can only shrink. The migration carriers under ``scripts/`` are temporary by
construction and their tests go with them; freezing both is what makes their
eventual removal show up as a deletion here rather than as nothing at all.

WIDENING THE DDL SCAN MADE THE GATE MATCH ITS OWN SOURCE
========================================================
``_DEFINES_A_TABLE`` is an unanchored ``\\bCREATE\\s+TABLE\\b`` over file TEXT,
so pointing it at ``tests/`` matches THIS FILE, whose prose says "CREATE
TABLE" a dozen times while explaining the rule. It also matches PostgreSQL
DDL in migration tests, and three modules that merely quote the phrase in a
docstring to say the schema issues ZERO of them. ``SCAN_EXEMPT`` carries
those, each with a written reason, and is itself under a staleness gate — an
exemption that stops matching must leave, or it becomes a blessed filename.

THE IMPORT SCAN CANNOT SEE A VENDORED SQLite — THE HOLE THAT WAS LIVE HERE
==========================================================================
``agents.SQLiteSession(...)`` opened a real SQLite file on disk, per agent, and
NOTHING in that call chain imported sqlite3 in this repo — the ``openai-agents``
package did it. An import-based scan is structurally blind to that, and it was
blind to it for the whole life of this file until the vendor scan landed on
2026-08-29. ``FROZEN_VENDOR_SQLITE`` freezes the constructs instead of the
import: ``SQLiteSession(``, ``SqliteDict(``, ``create_engine("sqlite...``,
``aiosqlite``, ``apsw``, ``libsql``, ``sqlite:///`` and
``.sqlite``/``.sqlite3`` path literals.

THAT SET IS NOW EMPTY, and the scan stays. The runner's conversation state
moved to PostgreSQL the day after the scan was written, so the population it
was built to measure is gone — which is exactly when a ratchet stops being
evidence and starts being the only thing standing between zero and the next
one. The set may only shrink; it has, to nothing.

``sqlite3.connect(`` is deliberately NOT among them. Any module calling it has
already imported sqlite3 and is therefore covered by ``FROZEN_SQLITE``; adding
it would only duplicate that coverage under a second name.

WHEN THE FOOTPRINT REACHES ZERO, DELETE THE SETS — NOT THIS FILE
================================================================
The terminal action for the import ratchet is to delete ``FROZEN_SQLITE`` and
its two tests (``test_no_module_outside_the_frozen_list_imports_sqlite`` and
``test_the_frozen_list_has_no_stale_entries``), and likewise for each sibling
set as its population empties. It is NOT to delete this file. The vendor scan
must outlive them: ``SQLiteSession`` is the shape SQLite comes back in once
nobody writes ``import sqlite3`` any more, and a repo with an empty
``FROZEN_SQLITE`` and no vendor scan is precisely the six-months-later state
the operator's instruction is about.

THE POSITIVE CONTROLS ARE PLANTED FILES, NOT THE LIVE TREE
==========================================================
They used to assert that the live tree still contained something to find. At
the end state of this migration the live tree is EMPTY BY DESIGN, so those
controls would have passed forever without ever being able to fail — an
unfailable control is worse than none, because the file still lists it. Each
control now writes a known-positive file into ``tmp_path`` and asserts the
scanner finds it there. The control exercises the SCANNER; the gates exercise
the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "scitex_agent_container"
SCRIPTS = REPO / "scripts"
TESTS = REPO / "tests"

#: The three scanned populations, and the anchor each set's paths are written
#: against. ``FROZEN_SQLITE`` / ``FROZEN_SQLITE_DDL`` / ``POSTGRES_DDL`` /
#: ``FROZEN_VENDOR_SQLITE`` are SRC-relative, the convention this file started
#: with and which is worth keeping because those entries are import paths a
#: reader recognises. Everything added for ``scripts/`` and ``tests/`` is
#: REPO-relative instead: ``SCAN_EXEMPT`` spans both roots, so a bare
#: ``test_state_db.py`` would be ambiguous, and a repo-relative path pastes
#: straight out of ``rg -l`` without a mental prefix step.

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
        # _lifecycle/_rename_db.py LEFT THIS SET 2026-08-29, by DELETION. It
        # was the last module here that opened SQLite as an ENGINE rather than
        # as a backend — it enumerated sqlite_master to rewrite an agent's
        # rows across every table — and the note below said it would leave
        # "when that rewrite happens, not before". That rewrite is done: every
        # table it walked is in PostgreSQL and renamed by its own step in
        # `_lifecycle/_rename`, the last of them `comms_grants`. The entry has
        # to go in the SAME commit as the file, because this list may only
        # shrink and an entry naming a file that no longer exists is a blessed
        # coordinate waiting for whatever drifts into its place.
        #
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
        # _state/port_allocator.py LEFT THIS SET 2026-08-28 — the a2a port
        # claim ledger, moved to per-host PostgreSQL via scitex_dev.store. It
        # is the first table whose move had to survive a HAZARD IN THE MAPPING
        # rather than only a missing verb: the store reads with
        # include_hidden=True on the write path, so the hide() that replaces
        # DELETE leaves a TOMBSTONE still occupying the identity. A naive
        # "holder is not None and not hidden" guard then refuses the SAME
        # agent's re-claim of its own PINNED port — and a pinned agent
        # restarts through exactly claim -> release -> re-claim, so that is
        # every pinned agent on the fleet one restart from staying down.
        # test_port_allocator_pin_reclaim.py measured the hazard BEFORE the
        # move and named the fix (unhide); port_allocator_store.try_claim is
        # where it lives.
        "_state/state_db.py",
        # _state/state_db_health.py LEFT THIS SET 2026-08-29, by DELETION and
        # not by a port. ``inspect_store`` classified a state.db as absent /
        # empty / populated so ``sac db show`` could say whether a zero meant
        # "no rows" or "wrong database" — the remedy for the 2026-08-09
        # incident named in this file's own prose. ``db show`` was its only
        # caller in src/ and went the same day with the rest of the SQLite
        # read surface, so the classifier had nothing left to report to. What
        # it was FOR is not lost: the reporting-boundary rule it established
        # is carried, by name, in ``_maintenance/_roster_state``.
        # _state/state_db_heartbeats.py LEFT THIS SET 2026-08-28, and it is
        # the first entry to leave by DELETION rather than by a port: its
        # table ``instance_heartbeats`` was removed from state.db, and the
        # module was the table's entire API — ``update_heartbeat`` (write)
        # and ``latest_instance_heartbeat`` (read), neither with a single
        # caller in src/, against 0 rows on every host measured. Nothing
        # moved to PostgreSQL because there was nothing to move.
        #
        # THE STALENESS GATE IS WHAT MAKES THIS EDIT MANDATORY rather than
        # optional, and that is the asymmetry this file was written for: a
        # deleted module cannot import sqlite3, so leaving the entry would
        # fail `test_the_frozen_list_has_no_stale_entries` — the footprint
        # shrinking is not allowed to go unrecorded.
        # _state/state_db_migrations.py LEFT THIS SET 2026-08-28 — and
        # it left by DELETION rather than by porting. Its last function
        # ALTERed ``instances``, which moved to the shared PostgreSQL
        # store; what remained was departure notes with no code and no
        # importer, whose ``import sqlite3`` existed only to keep this
        # entry honest. A module kept alive by the freeze list is the
        # freeze list holding the footprint UP, which is the opposite
        # of a ratchet.
        # _state/state_db_relocation.py LEFT THIS SET 2026-08-28 — the
        # relocation TRIO (agent_residency, relocation_leases,
        # relocation_journal) moved to PostgreSQL together, in one change, and
        # the module itself was DELETED rather than emptied: its three tables
        # now live in _state/relocation_pg.py via scitex_dev.store.
        #
        # ALL THREE OR NONE, and the reason is worth keeping because it is not
        # obvious from the tables alone. They shared this one module, so a
        # lease-only cutover would have left the fence being written to
        # PostgreSQL and read from SQLite — a handover that lands at fence 1
        # instead of 3 — while a residency-only one would have forked residency
        # from the journal that records the move which set it. Either half is a
        # split brain that raises nothing: some readers see a record, others do
        # not.
        #
        # It also carried TWO PIECES OF PROSE that became false at the cutover
        # and that no import check can see: cli_pkg/_relocate_readiness.py named
        # this module as where residency is written, and
        # _lifecycle/_relocate_checks_late.py told an operator to read the
        # relocation_leases row — at check_lease_holdable, the gate that stands
        # between one live agent and two. `sqlite3 state.db` still answers that
        # question, from the row this cutover left behind. Both now name the
        # store.
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


#: The measured SQLite footprint under ``scripts/`` on 2026-08-29, as
#: REPO-relative paths. THIS LIST MAY ONLY SHRINK.
#:
#: Every one of these is a MIGRATION CARRIER: it reads a table out of a
#: per-host ``state.db`` and writes it into PostgreSQL. They are the one place
#: in the repo where opening SQLite is the whole point of the file, and they
#: are temporary by construction — each one retires when its table's rows have
#: landed everywhere. Freezing them is what makes that retirement visible: a
#: carrier deleted without its entry deleted fails the staleness gate below.
#:
#: A NEW ENTRY HERE IS NOT AUTOMATICALLY FINE just because migrations are
#: expected. The shape to refuse is a carrier copied to move a table that is
#: already on PostgreSQL, or a script that opens ``state.db`` to ANSWER a
#: question rather than to drain it — the second is sac reading SQLite at
#: runtime wearing a script's filename.
#:
#: NOT IN THIS SET, and worth recording because the obvious guess is wrong:
#: ``scripts/migrate_inbound_dispatches_to_postgres.py`` (added 2026-08-29 in
#: #1267) does NOT import sqlite3. It reaches its source rows through
#: ``_migrate_lib``, and its only mention of the engine is the string
#: ``sqlite_id=`` in an operator-facing progress line. Its TEST does import
#: sqlite3 — to build the source database the carrier reads — so the entry
#: that exists for it lives in ``FROZEN_SQLITE_TESTS``, not here.
FROZEN_SQLITE_SCRIPTS = frozenset(
    {
        # The shared carrier library: opens the source ``state.db`` read-only
        # and hands rows to whichever migration imported it. Every entry below
        # depends on this one, so it is the LAST to leave, not the first.
        "scripts/_migrate_lib.py",
        "scripts/migrate_a2a_ports_to_postgres.py",
        "scripts/migrate_auth_state_to_postgres.py",
        "scripts/migrate_comms_grants_to_postgres.py",
        "scripts/migrate_diary_to_postgres.py",
        "scripts/migrate_dispatches_to_postgres.py",
        "scripts/migrate_incarnations_to_postgres.py",
        "scripts/migrate_node_comms_policy_to_postgres.py",
        "scripts/migrate_relocation_to_postgres.py",
        "scripts/migrate_verdict_delivered_to_postgres.py",
    }
)


#: The measured SQLite footprint under ``tests/`` on 2026-08-29, as
#: REPO-relative paths. THIS LIST MAY ONLY SHRINK.
#:
#: WHY POLICE TESTS AT ALL, given the docstring's "a ``:memory:`` fixture
#: creates nothing durable"? Because the two claims are different. Nothing
#: here forbids a test from touching SQLite; this set COUNTS the tests that
#: do, so the count cannot quietly go up. Two shapes make that worth doing:
#: a test is the easiest place to reintroduce an engine assumption after the
#: production code has left it, and a test file is the usual thing copied when
#: a new module is written in the old style.
#:
#: The split by root mirrors the two reasons a test is in here at all. The
#: ``develop/`` entries build a SQLite source database so a migration carrier
#: has something to drain — they retire with their carrier. The
#: ``scitex_agent_container/`` entries test code that still speaks SQLite —
#: they retire when that code does.
FROZEN_SQLITE_TESTS = frozenset(
    {
        # --- carrier tests: they CREATE a source state.db to be drained ---
        # The shared fixture kit behind the channel-migration tests.
        "tests/develop/_channel_migration_kit.py",
        "tests/develop/test_migrate_channel_events.py",
        "tests/develop/test_migrate_channel_events_overlap.py",
        # Added 2026-08-29 with the carrier from #1267. The carrier itself
        # does not import sqlite3 (see FROZEN_SQLITE_SCRIPTS); its test does,
        # because something has to write the rows the carrier then reads.
        "tests/develop/test_migrate_inbound_dispatches.py",
        # The cross-carrier guard that every migration is dry-run by default:
        # it opens the source db afterwards to prove nothing was written.
        "tests/develop/test_migrate_scripts_do_not_write_by_default.py",
        # --- tests of production code that still speaks SQLite ---
        # ``test__rename_db.py`` LEFT THIS SET 2026-08-29, by DELETION,
        # together with the src module it paired with. Its own control test
        # said what to do when the module ran out of work to describe: delete
        # it "along with its ``state-db`` step rather than kept as a loop over
        # an empty tuple". That is what happened.
        "tests/scitex_agent_container/_state/test_state_db.py",
        "tests/scitex_agent_container/_state/test_state_db_connect_branches.py",
        # tests/.../_state/test_state_db_health.py LEFT THIS SET 2026-08-29
        # with the module it tested. This is the ``develop/`` half of the same
        # asymmetry the docstring describes: a deleted test file cannot import
        # sqlite3, so leaving the entry would fail the staleness direction.
        "tests/scitex_agent_container/_state/test_state_db_instances.py",
        "tests/scitex_agent_container/_state/test_state_db_turns_errors_heartbeats.py",
    }
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
        #
        # _state/state_db_acl_policy.py LEFT THIS SET 2026-08-28 — the Phase-3
        # per-spec ACL table, and the first move where a lost write is a
        # PRIVILEGE change rather than a lost observation: read_comms_policy
        # answers a missing record with all-allow defaults, so an unreachable
        # store that returned empty instead of raising would read as
        # PERMISSION. It is also the first to leave this set on the strength
        # of its DOCSTRING: the module never executed DDL, it quoted the
        # CREATE TABLE in its prose, and this scan reads file text — so the
        # entry went stale the moment the prose was rewritten, exactly as the
        # two-directional rule intends.
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

#: Modules whose ``CREATE TABLE`` DDL is aimed at POSTGRESQL, not SQLite.
#:
#: This scan reads source as TEXT, so ``CREATE TABLE`` alone cannot say which
#: engine a statement targets — and since 2026-08-28 that distinction exists:
#: ``_state/state_db_channel_store.py`` carries two ``CREATE TABLE``
#: statements that are PostgreSQL (ADR-0023, the last table to leave SQLite).
#: Freezing it into ``FROZEN_SQLITE_DDL`` would put a lie in a list whose
#: entire value is being an accurate inventory of SQLite, and the staleness
#: gate would never take it back off: that gate only drops entries which stop
#: carrying DDL at all.
#:
#: AN EXPLICIT LIST RATHER THAN A DERIVED RULE, and that is a correction. The
#: first version discriminated by searching each file for ``host_store`` —
#: the resolver that has no SQLite fallback, so a module using it cannot be
#: issuing SQLite DDL. It matched ``_state/state_db_schema.py``, which is the
#: SQLite definer this whole file exists to police: that module's PROSE
#: mentions ``host_store`` twice while explaining where the moved tables
#: went. A comment describing PostgreSQL is PostgreSQL to a regex. The
#: negative control below is what caught it, and it is kept because the next
#: clever rule will fail the same way.
#:
#: THIS LIST MAY GROW — the opposite asymmetry from ``FROZEN_SQLITE_DDL``,
#: because more PostgreSQL is the direction of travel. What an addition must
#: carry is a REASON a human checked, that the DDL really is PostgreSQL.
POSTGRES_DDL = frozenset(
    {
        # ADR-0023: sac_channel_events + sac_channel_cursor, plain PostgreSQL
        # tables in the database ``host_store`` resolves to. Deliberately NOT
        # scitex_dev.store records — three measured disqualifiers in the ADR.
        "_state/state_db_channel_store.py",
    }
)


#: DDL matches under ``scripts/`` and ``tests/`` that are not SQLite growth,
#: as REPO-relative paths. Under the SAME staleness gate as the frozen sets:
#: an entry that stops matching must be deleted, or the exemption decays into
#: a blessed filename that a future file inherits.
#:
#: EVERY ENTRY CARRIES ITS REASON, and the reasons are not interchangeable —
#: "it is PostgreSQL DDL" and "it only says the words in a docstring" fail in
#: different directions, and a reader deciding whether a NEW entry belongs
#: needs to know which case they are looking at.
#:
#: Files already in ``FROZEN_SQLITE_SCRIPTS`` / ``FROZEN_SQLITE_TESTS`` are
#: NOT repeated here. They are unioned in by the gate, exactly as
#: ``FROZEN_SQLITE`` is unioned into the ``src/`` DDL gate: a file already
#: frozen as a SQLite opener is allowed to define the tables it opens.
SCAN_EXEMPT = frozenset(
    {
        # POSTGRESQL DDL. ``CREATE TABLE IF NOT EXISTS sac_channel_import`` —
        # the provenance ledger recording which id windows of
        # ``sac_channel_events`` came from an import. BIGINT / DOUBLE
        # PRECISION columns, created in the store this fleet is migrating TO.
        # Freezing it as SQLite would put a falsehood in an inventory.
        "scripts/_channel_import_provenance.py",
        # POSTGRESQL DDL. Creates ``store_reference`` and
        # ``sac_channel_events`` through psycopg against a live cluster to
        # test who ends up OWNING a migrated table.
        "tests/develop/test_migrate_channel_events_ownership.py",
        # POSTGRESQL DDL. ``_make_table`` builds throwaway relations in a
        # temporary schema via psycopg + ``generate_series`` — PostgreSQL-only
        # syntax, and the fixture for a bug about counting relations by name.
        "tests/develop/test_migrate_instances_verify_named_relation.py",
        # PROSE ONLY. The docstring says sac's ``init_schema`` now issues ZERO
        # ``CREATE TABLE``. A comment describing the ABSENCE of DDL is DDL to
        # an unanchored regex — the same trap that once made a ``host_store``
        # heuristic excuse ``_state/state_db_schema.py``.
        "tests/scitex_agent_container/_helpers/fleet_root.py",
        # PROSE ONLY. Same sentence, same reason: it explains why a SELECT
        # against a freshly created state.db cannot find a table.
        "tests/scitex_agent_container/_lifecycle/test__rename.py",
        # PROSE ONLY. Names ``CREATE TABLE lineage`` in a docstring to say
        # that a stray one sneaking back into the schema is what this test
        # exists to catch. The gate must not read a guard as the thing it
        # guards against.
        "tests/scitex_agent_container/_state/test_state_db_nodes.py",
        # THIS FILE. Widening the DDL scan to ``tests/`` makes the gate match
        # its own source, twice over: the prose above says "CREATE TABLE" a
        # dozen times while explaining the rule, and the planted-file control
        # writes a literal ``CREATE TABLE`` into tmp_path to prove the scanner
        # works. Listed EXPLICITLY rather than special-cased inside the
        # scanner, because a scanner that silently skips one path is a scanner
        # nobody can audit — and the one path it would skip is the gate.
        "tests/develop/test_sqlite_footprint_frozen.py",
    }
)


# A SQLite database opened WITHOUT importing sqlite3 — the hole the import
# scan cannot see by construction, and the one that was live for the whole
# life of this file. ``agents.SQLiteSession(...)`` writes a real per-agent
# database under ~/.scitex/agent-container/runtime/openai-sessions/; the
# sqlite3 import happens inside ``openai-agents``, not here.
#
# ``sqlite3.connect(`` is deliberately absent: it is not a VENDOR construct,
# and every module that calls it necessarily imports sqlite3, so
# FROZEN_SQLITE already holds it. Adding it here would double-count the
# modules the import scan already covers without catching anything new.
_CONSTRUCTS_VENDOR_SQLITE = re.compile(
    r"SQLiteSession\s*\(|"
    r"SqliteDict\s*\(|"
    r"create_engine\s*\(\s*.{0,3}sqlite|"
    r"aiosqlite|"
    r"\bapsw\b|"
    r"\blibsql\b|"
    r"sqlite:///|"
    r"\.sqlite3?\b"
)

#: Modules under ``src/`` that open SQLite through a VENDOR library rather
#: than through sqlite3, as SRC-relative paths. THIS LIST MAY ONLY SHRINK.
#:
#: EMPTY SINCE 2026-08-29, and it took one day to get here. The scan was
#: written that morning and found TWO entries that had been invisible to every
#: other gate in this file — concrete evidence that "no module imports
#: sqlite3" was never the same statement as "no module opens SQLite". Both are
#: gone the same day:
#:
#:   * ``_runners/openai_session.py`` constructed
#:     ``agents.SQLiteSession(self.session_id, db_path=str(db_path))`` for the
#:     OpenAI runner's conversation state. It now constructs
#:     ``_runners._openai_pg_session.PostgresAgentSession``, which keeps the
#:     same conversation in this host's PostgreSQL through
#:     ``_state/openai_session_store.py``.
#:   * ``runtimes/_openai_sdk_common.py`` resolved WHERE that database lived.
#:     A store target is not a path, so the helper was deleted rather than
#:     rewritten — along with the module docstring's cross-reference to it.
#:
#: The scan itself STAYS at zero. ``test_the_vendor_list_has_no_stale_entries``
#: keeps this set honest in the other direction, and the two positive controls
#: below are planted files precisely so an empty live tree cannot make them
#: vacuous.
FROZEN_VENDOR_SQLITE: frozenset[str] = frozenset()


def _modules_defining_tables(root: Path) -> set[str]:
    """Every file under ``root`` carrying CREATE TABLE DDL, ``root``-relative.

    Deliberately NOT excluding modules that also import sqlite3 — a module can
    legitimately be in both sets, and subtracting one from the other would
    make each list depend on the other's accuracy.

    ``root`` is a PARAMETER rather than the hard-coded ``SRC`` it used to be:
    the same rule has to reach ``scripts/`` and ``tests/``, and a second
    copy of the walk would be a second thing to keep in step.
    """
    return _scan(root, _DEFINES_A_TABLE)


def _modules_defining_postgres_tables() -> set[str]:
    """The declared PostgreSQL definers that STILL carry DDL.

    Intersected with the scan rather than returned raw, so an entry that
    stops defining a table cannot go on excusing the file it names.
    """
    return POSTGRES_DDL & _modules_defining_tables(SRC)


def _modules_importing_sqlite(root: Path) -> set[str]:
    """Every file under ``root`` that imports sqlite3, ``root``-relative."""
    return _scan(root, _IMPORTS_SQLITE)


def _modules_constructing_vendor_sqlite(root: Path) -> set[str]:
    """Every file under ``root`` opening SQLite via a vendor library."""
    return _scan(root, _CONSTRUCTS_VENDOR_SQLITE)


def _scan(root: Path, pattern: re.Pattern[str]) -> set[str]:
    """Paths under ``root`` whose SOURCE TEXT matches ``pattern``.

    Text, not AST, and that is deliberate in both directions. It is why a
    docstring quoting ``CREATE TABLE`` matches (hence ``SCAN_EXEMPT``), and it
    is also why the vendor scan can see ``agents.SQLiteSession(`` without
    resolving what ``agents`` is bound to at runtime.
    """
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(path.relative_to(root).as_posix())
    return found


def _repo_relative(root: Path, names: set[str]) -> set[str]:
    """Re-anchor ``root``-relative scan output at the repository root."""
    prefix = root.relative_to(REPO).as_posix()
    return {f"{prefix}/{name}" for name in names}


def _files_outside_src_defining_tables() -> set[str]:
    """CREATE TABLE matches under ``scripts/`` and ``tests/``, repo-relative."""
    found: set[str] = set()
    for root in (SCRIPTS, TESTS):
        found |= _repo_relative(root, _modules_defining_tables(root))
    return found


def test_the_import_scan_finds_a_planted_import(tmp_path: Path) -> None:
    """POSITIVE CONTROL — the SCANNER works, proved on a file we planted.

    THIS USED TO ASSERT AGAINST THE LIVE TREE, and that was a control with an
    expiry date. Its message even said so: "either every SQLite user was
    removed (delete this file) or the scan is broken". At the end state of
    this migration the first branch is the DESIGNED outcome, so a live-tree
    control passes forever and can no longer fail — an unfailable check that
    the config still lists, which is worse than no check because it reads as
    coverage.

    A planted file separates the two questions the old control conflated. This
    one asks "does the scanner find what is there", which stays answerable at
    zero. The gates below ask "what is there", which is allowed to reach zero.
    """
    # Arrange — a subdirectory too, so a broken rglob cannot pass by luck.
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("import sqlite3\n", encoding="utf-8")
    (tmp_path / "innocent.py").write_text("x = 1\n", encoding="utf-8")
    # Act
    found = _modules_importing_sqlite(tmp_path)
    # Assert
    assert found == {"pkg/planted.py"}, (
        "the sqlite3 import scan did not find a planted `import sqlite3` at "
        f"pkg/planted.py; it returned {sorted(found)}. Every gate below is "
        "vacuous until this passes."
    )


def test_no_module_outside_the_frozen_list_imports_sqlite() -> None:
    """The operator's rule: SQLite must not grow back."""
    # Arrange
    found = _modules_importing_sqlite(SRC)
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
    found = _modules_importing_sqlite(SRC)
    # Act
    stale = sorted(FROZEN_SQLITE - found)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE lists modules that no longer import sqlite3: "
        f"{stale}. Good news — the footprint shrank. Delete these entries so "
        "the list keeps describing reality; a stale allowlist re-opens the "
        "door it was written to close. When the set empties, delete "
        "FROZEN_SQLITE and its two import tests — NOT this file, which still "
        "owns the vendor scan."
    )


# ----------------------------------------------------------------------
# The same rule over scripts/ and tests/ — populations no gate could see
# until 2026-08-29, and together larger than what remains under src/.
# ----------------------------------------------------------------------


def test_no_script_outside_the_frozen_list_imports_sqlite() -> None:
    """A migration carrier is expected. A NEW one is a decision."""
    # Arrange
    found = _repo_relative(SCRIPTS, _modules_importing_sqlite(SCRIPTS))
    # Act
    new = sorted(found - FROZEN_SQLITE_SCRIPTS)
    # Assert
    assert not new, (
        "NEW SQLITE UNDER scripts/. These scripts import sqlite3 and are not "
        f"in the frozen footprint: {new}. The legitimate shape is a carrier "
        "that DRAINS a state.db table into PostgreSQL and then retires. A "
        "script that opens state.db to ANSWER a question is sac reading "
        "SQLite at runtime wearing a script's filename — raise it."
    )


def test_the_frozen_script_list_has_no_stale_entries() -> None:
    """A retired carrier must take its entry with it."""
    # Arrange
    found = _repo_relative(SCRIPTS, _modules_importing_sqlite(SCRIPTS))
    # Act
    stale = sorted(FROZEN_SQLITE_SCRIPTS - found)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE_SCRIPTS lists scripts that no longer import sqlite3: "
        f"{stale}. These carriers are meant to retire — delete the entries so "
        "the deletion is recorded rather than leaving a blessed filename a "
        "future script can inherit."
    )


def test_no_test_outside_the_frozen_list_imports_sqlite() -> None:
    """Counting the tests that touch SQLite, not forbidding them."""
    # Arrange
    found = _repo_relative(TESTS, _modules_importing_sqlite(TESTS))
    # Act
    new = sorted(found - FROZEN_SQLITE_TESTS)
    # Assert
    assert not new, (
        "NEW SQLITE UNDER tests/. These tests import sqlite3 and are not in "
        f"the frozen footprint: {new}. This set does not forbid a test from "
        "touching SQLite — it stops the count going up quietly. If the test "
        "covers a carrier, it retires with the carrier; if it covers "
        "production code that still speaks SQLite, that code is the thing to "
        "raise."
    )


def test_the_frozen_test_list_has_no_stale_entries() -> None:
    """A test that stopped using SQLite must leave the list."""
    # Arrange
    found = _repo_relative(TESTS, _modules_importing_sqlite(TESTS))
    # Act
    stale = sorted(FROZEN_SQLITE_TESTS - found)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE_TESTS lists tests that no longer import sqlite3: "
        f"{stale}. Delete the entries — the list is an inventory, not a set "
        "of blessed filenames."
    )


# ----------------------------------------------------------------------
# The DDL footprint — the hole the import check could not see.
# ----------------------------------------------------------------------


def test_the_ddl_scan_finds_a_planted_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL — the DDL SCANNER works, on a file we planted.

    Same correction as the import control above, for the same reason: the live
    tree is allowed to reach zero CREATE TABLE, so a live-tree assertion here
    would stop being able to fail exactly when the migration succeeded.
    """
    # Arrange
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('DDL = "CREATE TABLE t (a int);"\n', encoding="utf-8")
    (tmp_path / "innocent.py").write_text("x = 1\n", encoding="utf-8")
    # Act
    found = _modules_defining_tables(tmp_path)
    # Assert
    assert found == {"pkg/planted.py"}, (
        "the DDL scan did not find a planted CREATE TABLE at pkg/planted.py; "
        f"it returned {sorted(found)}. Every DDL gate below is vacuous until "
        "this passes."
    )


def test_no_module_defines_a_new_sqlite_table() -> None:
    """A module may not DEFINE a SQLite table without being frozen.

    This is the case `import sqlite3` cannot see, and it is the idiomatic way
    tables are added here — five existing modules do exactly this. Without
    this gate, `state_db_<newthing>.py` grows SQLite back on a green build.
    """
    # Arrange
    frozen = FROZEN_SQLITE_DDL | FROZEN_SQLITE | _modules_defining_postgres_tables()
    # Act
    new = sorted(_modules_defining_tables(SRC) - frozen)
    # Assert
    assert not new, (
        "these modules define SQLite tables and are not frozen: "
        f"{new}. sac is migrating state to PostgreSQL via scitex_dev.store; "
        "a new SQLite table is a decision to raise, not a line to add to "
        "FROZEN_SQLITE_DDL."
    )


def test_the_postgres_exemption_finds_something() -> None:
    """POSITIVE CONTROL — an empty exemption set would make the gate vacuous
    in the OTHER direction, and a broken regex is exactly how that happens.

    Distinct from the control above: that one proves the DDL scan sees
    anything at all; this one proves the discriminator that EXCUSES a module
    still matches something. Both are needed, because a gate can fail by
    finding nothing and by excusing everything.
    """
    # Arrange
    scanned_root = SRC
    # Act
    found = _modules_defining_postgres_tables()
    # Assert
    assert found, (
        f"no declared PostgreSQL definer still carries DDL under {scanned_root} — "
        "either POSTGRES_DDL went stale or the scan broke"
    )


def test_the_postgres_exemption_does_not_excuse_the_sqlite_schema() -> None:
    """NEGATIVE CONTROL — the exemption must not swallow the real definer.

    ``_state/state_db_schema.py`` owns the remaining SQLite tables. If it ever
    landed in the exemption set, the gate above would go green while SQLite
    DDL grew freely, which is the precise failure this whole file exists to
    prevent. An exemption that can excuse the thing being policed is not an
    exemption, it is a hole.

    NOT HYPOTHETICAL: the first version of the exemption derived membership by
    searching each file for ``host_store``, and it DID match this module —
    whose prose mentions the resolver twice while explaining where the moved
    tables went. This assertion is what reported it, which is why the
    exemption is now an explicit list.
    """
    # Arrange
    the_sqlite_definer = "_state/state_db_schema.py"
    # Act
    exempt = _modules_defining_postgres_tables()
    # Assert
    assert the_sqlite_definer not in exempt


def test_the_ddl_freeze_list_has_no_stale_entries() -> None:
    """An entry that no longer defines a table must leave the list.

    Same asymmetry as the import list: shrinking is two lines, and a stale
    allowlist rots into blessed filenames that a future module inherits.
    """
    # Arrange
    defining = _modules_defining_tables(SRC)
    # Act
    stale = sorted(FROZEN_SQLITE_DDL - defining)
    # Assert
    assert not stale, (
        "FROZEN_SQLITE_DDL lists modules that no longer define a table: "
        f"{stale}. Delete the entries — the list is an inventory, not a "
        "set of blessed filenames."
    )


# ----------------------------------------------------------------------
# The DDL scan over scripts/ and tests/ — where it matches PostgreSQL,
# matches prose about the absence of DDL, and matches this file.
# ----------------------------------------------------------------------


def test_no_file_outside_src_defines_an_unfrozen_table() -> None:
    """CREATE TABLE under scripts/ or tests/ must be accounted for.

    A migration carrier's test writes SQLite DDL because it has to build the
    database the carrier drains — legitimate, and already frozen by its import
    entry. What must not pass silently is a NEW file defining a SQLite table
    outside src/, which is the same growth the src/ gate refuses, one
    directory across.
    """
    # Arrange
    frozen = SCAN_EXEMPT | FROZEN_SQLITE_SCRIPTS | FROZEN_SQLITE_TESTS
    # Act
    new = sorted(_files_outside_src_defining_tables() - frozen)
    # Assert
    assert not new, (
        "these files under scripts/ or tests/ contain CREATE TABLE and are "
        f"not accounted for: {new}. If it is SQLite growth, that is a "
        "decision to raise. If it is PostgreSQL DDL, or a docstring merely "
        "quoting the phrase, add it to SCAN_EXEMPT WITH THE REASON — an "
        "exemption whose reason nobody wrote down cannot be re-checked."
    )


def test_the_scan_exemptions_have_no_stale_entries() -> None:
    """An exemption that stops matching must leave.

    Held to the SAME standard as the freeze lists, and for a sharper reason:
    a stale freeze entry permits a filename, and a stale exemption permits a
    filename that the gate has already been told to ignore. Both rot into
    blessed names; the exemption rots faster, because it is the list a reader
    skims past.
    """
    # Arrange
    defining = _files_outside_src_defining_tables()
    # Act
    stale = sorted(SCAN_EXEMPT - defining)
    # Assert
    assert not stale, (
        "SCAN_EXEMPT excuses files that no longer contain CREATE TABLE: "
        f"{stale}. Delete the entries; each one is now permission granted to "
        "a path rather than to a reason."
    )


def test_the_exemptions_never_overlap_the_frozen_sets() -> None:
    """NEGATIVE CONTROL — SCAN_EXEMPT must not swallow a real SQLite user.

    The sibling of ``test_the_postgres_exemption_does_not_excuse_the_sqlite_
    schema``, and the same failure shape: an exemption list that grows to
    cover the files being policed turns the gate green while the footprint
    grows. ``tests/develop/_channel_migration_kit.py`` is the concrete case —
    it really does CREATE SQLite tables, and it is accounted for by its
    ``FROZEN_SQLITE_TESTS`` entry, which shrinks under a staleness gate, not
    by an exemption, which is a standing pass.

    Stated as DISJOINTNESS rather than as a list of files to keep out, so it
    keeps meaning something as the frozen sets change. A named-file assertion
    would go quietly vacuous the day that file is deleted; this one holds for
    whatever is in the sets at the time.
    """
    # Arrange
    frozen = FROZEN_SQLITE_SCRIPTS | FROZEN_SQLITE_TESTS
    # Act
    overlap = sorted(frozen & SCAN_EXEMPT)
    # Assert
    assert not overlap, (
        "SCAN_EXEMPT excuses files that are also frozen SQLite users: "
        f"{overlap}. A file cannot be both — the frozen entry is an inventory "
        "line that must shrink, and the exemption is a standing pass that "
        "would outlive it."
    )


# ----------------------------------------------------------------------
# The vendor footprint — SQLite opened without importing sqlite3.
# ----------------------------------------------------------------------


def test_the_vendor_scan_finds_a_planted_construction(tmp_path: Path) -> None:
    """POSITIVE CONTROL (walk) — the vendor scanner reaches nested files."""
    # Arrange
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("s = agents.SQLiteSession('x')\n", encoding="utf-8")
    (tmp_path / "innocent.py").write_text("x = 1\n", encoding="utf-8")
    # Act
    found = _modules_constructing_vendor_sqlite(tmp_path)
    # Assert
    assert found == {"pkg/planted.py"}, (
        "the vendor scan did not find a planted SQLiteSession construction at "
        f"pkg/planted.py; it returned {sorted(found)}."
    )


def test_the_vendor_regex_matches_the_real_construction() -> None:
    """POSITIVE CONTROL (regex) — against the live call, character for
    character.

    Separate from the walk control on purpose. The walk proves the scanner
    visits files; this proves the PATTERN matches the thing it was written
    for. A regex can be narrowed by a well-meant edit — an anchor, a word
    boundary — and still walk the whole tree finding nothing.
    """
    # Arrange — the call this scan was written for, as it stood at
    # src/scitex_agent_container/_runners/openai_session.py:413 before it
    # moved to PostgreSQL. A LITERAL, not a read of the tree: the tree is
    # empty by design now, so anchoring this to a live line would delete the
    # only proof the pattern still matches the shape it exists to catch.
    the_live_call = "agents.SQLiteSession(self.session_id, db_path=str(db_path))"
    # Act
    hit = _CONSTRUCTS_VENDOR_SQLITE.search(the_live_call)
    # Assert
    assert hit is not None, (
        "the vendor pattern no longer matches the construction it exists to "
        f"catch: {the_live_call!r}. The gate below is vacuous."
    )


def test_the_vendor_regex_does_not_match_a_docstring_mention() -> None:
    """NEGATIVE CONTROL — prose ABOUT SQLiteSession must not match.

    Measured 2026-08-29, on the tree this scan was written against: SEVEN
    files under src/ named ``SQLiteSession`` and exactly ONE constructed it.
    Had the pattern dropped the ``\\(`` and matched the bare name, the frozen
    set would have grown sevenfold to hold every module that merely
    DOCUMENTED the session db — and a list of documenters is not an inventory
    of databases. This is the same inversion that once let a ``host_store``
    heuristic excuse the real SQLite schema module: a comment describing a
    thing is that thing, to a regex.

    All seven are gone now — the construction moved to PostgreSQL and the six
    prose mentions were reworded with it — so this control is asserted on a
    literal rather than on the tree. Six documenters could not be told from
    one database by any live-tree assertion once the tree reaches zero.
    """
    # Arrange — the prose that stood at
    # src/scitex_agent_container/_runners/_openai_session_cli.py:21 before the
    # migration reworded it. Kept as a literal for the same reason as the
    # positive control above.
    the_prose = "(the ``SQLiteSession`` db persists turns under the agent's own name,"
    # Act
    hit = _CONSTRUCTS_VENDOR_SQLITE.search(the_prose)
    # Assert
    assert hit is None, (
        f"the vendor pattern matched PROSE, at {hit.group(0)!r} in "
        f"{the_prose!r}. A scan that reads documentation as usage inverts: "
        "the set stops being an inventory of who opens a database."
    )


def test_no_module_outside_the_vendor_list_opens_sqlite() -> None:
    """SQLite through a vendor library is still SQLite."""
    # Arrange
    found = _modules_constructing_vendor_sqlite(SRC)
    # Act
    new = sorted(found - FROZEN_VENDOR_SQLITE)
    # Assert
    assert not new, (
        "NEW VENDORED SQLITE. These modules open a SQLite database through a "
        f"third-party library rather than through sqlite3: {new}. No import "
        "check can see this — the sqlite3 import happens inside the vendor "
        "package — which is exactly why it is frozen separately. A new "
        "per-agent SQLite file is the same decision as a new state.db, "
        "whoever writes the connect()."
    )


def test_the_vendor_list_has_no_stale_entries() -> None:
    """The vendor list is an inventory too."""
    # Arrange
    found = _modules_constructing_vendor_sqlite(SRC)
    # Act
    stale = sorted(FROZEN_VENDOR_SQLITE - found)
    # Assert
    assert not stale, (
        "FROZEN_VENDOR_SQLITE lists modules that no longer construct a vendor "
        f"SQLite database: {stale}. Delete the entries."
    )
