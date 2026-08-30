"""SQLite must not come back. The footprint is ZERO and the gates stay.

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
each grew the same mixed SQLite/PostgreSQL problem independently, which is the
evidence that intent alone does not hold this line.

THE ALLOWLISTS ARE GONE, AND THAT IS THE MIGRATION FINISHING
============================================================
This file used to carry four frozen sets — ``FROZEN_SQLITE`` (modules under
``src/`` importing sqlite3), ``FROZEN_SQLITE_SCRIPTS``, ``FROZEN_SQLITE_TESTS``
and ``FROZEN_SQLITE_DDL`` (modules defining SQLite tables without opening a
connection). Each was a measured inventory that could only shrink, so that
removing SQLite was a two-line change and adding it was a conversation.

Every one of those populations is now EMPTY, and an allowlist with no members
is not a weaker gate — it is a stronger and simpler one. The predicate

    every module under src/, scripts/ and tests/ that imports sqlite3
    appears in FROZEN_SQLITE

collapses, once the set is empty, into

    nothing imports sqlite3

which is the rule the operator actually asked for, with no list standing
between the reader and it. The staleness tests that kept each inventory honest
went with their sets: "no entry names a file that stopped matching" is
vacuously true of an empty set, and a test that cannot fail is worse than no
test because the file still lists it.

WHAT REPLACED THEM IS NOT NOTHING. The three scans below are unchanged and
still run on every push; only their exemption is. What was "these fourteen
files may, nothing else" is now "nothing may".

WHAT THIS FILE STILL CLAIMS, AND WHAT IT DOES NOT
=================================================
It does not forbid reading somebody else's ``.db`` file, and it does not
police the storage decisions of packages sac merely imports. It asserts three
properties of THIS repository: nothing imports sqlite3, nothing defines a
table that has not been declared PostgreSQL, and nothing opens SQLite through
a vendor library.

THE IMPORT CHECK ALONE WAS NEVER ENOUGH, TWICE OVER
===================================================
``import sqlite3`` is a good proxy for "somebody OPENED a database". It is not
a proxy for "somebody DEFINED a table": for most of this migration the
idiomatic way to add a table was to write a ``state_db_<thing>.py`` holding
DDL and hand it to a shared connection factory, importing nothing. Six modules
were shaped that way when the DDL scan was written. A new module written in
that style would grow SQLite back with the import gate still green, which is
precisely the six-months-later outcome the instruction above is about. The
``CREATE TABLE`` scan closes it, and ``POSTGRES_DDL`` is how a legitimately
PostgreSQL definer says so.

It is also not a proxy for "somebody opened SQLite through a LIBRARY".
``agents.SQLiteSession(...)`` wrote a real per-agent database on disk and
nothing in that call chain imported sqlite3 in this repo — the ``openai-agents``
package did. An import-based scan is structurally blind to that, and was blind
to it for the whole life of this file until the vendor scan landed on
2026-08-29 and immediately found two live entries. ``SQLiteSession`` is the
shape SQLite comes back in once nobody writes ``import sqlite3`` any more, so
the vendor scan is the one gate here that MUST outlive the others.

THE POSITIVE CONTROLS ARE PLANTED FILES, NOT THE LIVE TREE
==========================================================
They used to assert that the live tree still contained something to find. The
tree is now empty by design, so a live-tree control would pass forever without
ever being able to fail — an unfailable control the file still lists, which
reads as coverage. Each control writes a known-positive file into ``tmp_path``
and asserts the scanner finds it there. The controls exercise the SCANNERS;
the gates exercise the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "scitex_agent_container"
SCRIPTS = REPO / "scripts"
TESTS = REPO / "tests"

#: The three scanned roots. Gate messages and ``SCAN_EXEMPT`` are written in
#: REPO-relative paths, which paste straight out of ``rg -l`` and stay
#: unambiguous across roots; ``POSTGRES_DDL`` stays SRC-relative because its
#: entries are import paths a reader recognises.
SCANNED_ROOTS = (SRC, SCRIPTS, TESTS)


_IMPORTS_SQLITE = re.compile(
    r"^\s*(?:import\s+sqlite3\b|from\s+sqlite3\s+import\b)", re.M
)

_DEFINES_A_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.I)

#: Modules whose ``CREATE TABLE`` DDL is aimed at POSTGRESQL.
#:
#: This scan reads source as TEXT, so ``CREATE TABLE`` alone cannot say which
#: engine a statement targets. Freezing a PostgreSQL definer into a SQLite
#: inventory would put a lie in a list whose entire value is being accurate.
#:
#: AN EXPLICIT LIST RATHER THAN A DERIVED RULE, and that is a correction worth
#: keeping. The first version discriminated by searching each file for
#: ``host_store`` — the resolver that has no local-file fallback, so a module
#: using it cannot be issuing SQLite DDL. It matched the very module this file
#: existed to police, whose PROSE mentioned ``host_store`` twice while
#: explaining where the moved tables went. A comment describing PostgreSQL is
#: PostgreSQL to a regex. The negative control below is what caught it, and it
#: is kept because the next clever rule will fail the same way.
#:
#: THIS LIST MAY GROW — the opposite asymmetry from the retired SQLite
#: inventories, because more PostgreSQL is the direction of travel. What an
#: addition must carry is a REASON a human checked, that the DDL really is
#: PostgreSQL.
POSTGRES_DDL = frozenset(
    {
        # ADR-0023: sac_channel_events + sac_channel_cursor, plain PostgreSQL
        # tables in the database ``host_store`` resolves to. Deliberately NOT
        # scitex_dev.store records — three measured disqualifiers in the ADR.
        "_state/state_db_channel_store.py",
    }
)


#: ``CREATE TABLE`` text matches that are not a table definition at all, as
#: REPO-relative paths. Under a staleness gate: an entry that stops matching
#: must be deleted, or the exemption decays into a blessed filename that a
#: future file inherits.
#:
#: EVERY ENTRY CARRIES ITS REASON. "It is PostgreSQL DDL" and "it only says
#: the words in a docstring" fail in different directions, and a reader
#: deciding whether a NEW entry belongs needs to know which case they are
#: looking at. Today every entry is the second kind — prose about the absence
#: of DDL, which an unanchored regex reads as DDL.
SCAN_EXEMPT = frozenset(
    {
        # PROSE ONLY. Explains why a SELECT against a freshly created database
        # cannot find a table.
        "tests/scitex_agent_container/_helpers/fleet_root.py",
        # PROSE ONLY. Same sentence, same reason.
        "tests/scitex_agent_container/_lifecycle/test__rename.py",
        # PROSE ONLY. Names ``CREATE TABLE lineage`` in a comment to say that
        # a stray one sneaking back is what the retired test there existed to
        # catch. The gate must not read a guard as the thing it guards
        # against.
        "tests/scitex_agent_container/_state/test_state_db_nodes.py",
        # THIS FILE. The DDL scan reaches ``tests/`` and so matches its own
        # source twice over: the prose above says "CREATE TABLE" while
        # explaining the rule, and the planted-file control writes a literal
        # one into tmp_path to prove the scanner works. Listed EXPLICITLY
        # rather than special-cased inside the scanner, because a scanner that
        # silently skips one path is a scanner nobody can audit — and the one
        # path it would skip is the gate.
        "tests/develop/test_sqlite_footprint_frozen.py",
    }
)


# A SQLite database opened WITHOUT importing sqlite3 — the hole the import
# scan cannot see by construction, and the one that was live for the whole
# life of this file. ``agents.SQLiteSession(...)`` wrote a real per-agent
# database under ~/.scitex/agent-container/runtime/openai-sessions/; the
# sqlite3 import happened inside ``openai-agents``, not here.
#
# ``sqlite3.connect(`` is deliberately absent: it is not a VENDOR construct,
# and every module that calls it necessarily imports sqlite3, so the import
# gate already covers it. Adding it here would double-count without catching
# anything new.
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


def _modules_defining_tables(root: Path) -> set[str]:
    """Every file under ``root`` carrying CREATE TABLE DDL, ``root``-relative."""
    return _scan(root, _DEFINES_A_TABLE)


def _modules_importing_sqlite(root: Path) -> set[str]:
    """Every file under ``root`` that imports sqlite3, ``root``-relative."""
    return _scan(root, _IMPORTS_SQLITE)


def _modules_constructing_vendor_sqlite(root: Path) -> set[str]:
    """Every file under ``root`` opening SQLite via a vendor library."""
    return _scan(root, _CONSTRUCTS_VENDOR_SQLITE)


def _modules_defining_postgres_tables() -> set[str]:
    """The declared PostgreSQL definers that STILL carry DDL.

    Intersected with the scan rather than returned raw, so an entry that stops
    defining a table cannot go on excusing the file it names.
    """
    return POSTGRES_DDL & _modules_defining_tables(SRC)


def _all_files_defining_tables() -> set[str]:
    """CREATE TABLE matches across every scanned root, repo-relative."""
    found: set[str] = set()
    for root in SCANNED_ROOTS:
        found |= _repo_relative(root, _modules_defining_tables(root))
    return found


def _all_files_importing_sqlite() -> set[str]:
    """sqlite3 imports across every scanned root, repo-relative."""
    found: set[str] = set()
    for root in SCANNED_ROOTS:
        found |= _repo_relative(root, _modules_importing_sqlite(root))
    return found


# ----------------------------------------------------------------------
# The import footprint — nothing may import sqlite3.
# ----------------------------------------------------------------------


def test_the_import_scan_finds_a_planted_import(tmp_path: Path) -> None:
    """POSITIVE CONTROL — the SCANNER works, proved on a file we planted.

    THIS USED TO ASSERT AGAINST THE LIVE TREE, and that was a control with an
    expiry date. Its message even said so: "either every SQLite user was
    removed (delete this file) or the scan is broken". The first branch is now
    the DESIGNED outcome, so a live-tree control passes forever and can no
    longer fail.

    A planted file separates the two questions the old control conflated:
    whether the scanner works, and whether the tree is clean. This one answers
    the first, and the gate below answers the second.
    """
    # Arrange
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("import sqlite3\n", encoding="utf-8")
    (tmp_path / "innocent.py").write_text("x = 1\n", encoding="utf-8")
    # Act
    found = _modules_importing_sqlite(tmp_path)
    # Assert
    assert found == {"pkg/planted.py"}, (
        "the import scan did not find a planted `import sqlite3` at "
        f"pkg/planted.py; it returned {sorted(found)}. Every gate below is "
        "vacuous until this passes."
    )


def test_nothing_in_the_repository_imports_sqlite() -> None:
    """The whole rule, with no allowlist left to read around it."""
    # Arrange
    found = _all_files_importing_sqlite()
    # Act
    offenders = sorted(found)
    # Assert
    assert not offenders, (
        "NEW SQLITE. These files import sqlite3: "
        f"{offenders}. sac's state is the per-host PostgreSQL store reached "
        "through scitex_dev.store (ADR-0022); the fleet is MULTI-HOST, and a "
        "database file per host means a different truth per host. This is not "
        "a lint failure to silence with an entry in a list — there is no list "
        "any more, and re-adding one is the decision, not the diff."
    )


# ----------------------------------------------------------------------
# The DDL footprint — a table may be defined only if it is PostgreSQL.
# ----------------------------------------------------------------------


def test_the_ddl_scan_finds_a_planted_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL — the DDL scanner works, on a file we planted."""
    # Arrange
    planted = tmp_path / "pkg" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('SCHEMA = "CREATE TABLE t (a INTEGER)"\n', encoding="utf-8")
    (tmp_path / "innocent.py").write_text("x = 1\n", encoding="utf-8")
    # Act
    found = _modules_defining_tables(tmp_path)
    # Assert
    assert found == {"pkg/planted.py"}, (
        "the DDL scan did not find a planted CREATE TABLE at pkg/planted.py; "
        f"it returned {sorted(found)}."
    )


def test_no_file_defines_an_undeclared_table() -> None:
    """A table definition must be declared PostgreSQL or be prose."""
    # Arrange
    declared = _repo_relative(SRC, set(POSTGRES_DDL))
    # Act
    undeclared = sorted(_all_files_defining_tables() - declared - SCAN_EXEMPT)
    # Assert
    assert not undeclared, (
        "UNDECLARED TABLE DEFINITION. These files carry CREATE TABLE DDL that "
        f"is neither declared PostgreSQL nor a known prose match: {undeclared}. "
        "This is the shape SQLite grew in for most of this migration — DDL in "
        "a module that imports nothing and hands its schema to a shared "
        "connection factory — which the import gate above cannot see. If the "
        "DDL really is PostgreSQL, add it to POSTGRES_DDL with the reason you "
        "checked."
    )


def test_the_postgres_declaration_finds_something() -> None:
    """POSITIVE CONTROL — POSTGRES_DDL names files that really carry DDL.

    Without this, a typo in an entry silently excuses nothing while looking
    like it excuses something, and the gate above would go red for a reason
    the message could not explain.
    """
    # Arrange
    declared = set(POSTGRES_DDL)
    # Act
    live = _modules_defining_postgres_tables()
    # Assert
    assert live == declared, (
        "POSTGRES_DDL names files that no longer carry CREATE TABLE DDL: "
        f"{sorted(declared - live)}. An exemption that stops matching must be "
        "deleted, or it becomes a blessed filename the next file inherits."
    )


def test_the_scan_exemptions_have_no_stale_entries() -> None:
    """SCAN_EXEMPT is an inventory, not a wish."""
    # Arrange
    found = _all_files_defining_tables()
    # Act
    stale = sorted(SCAN_EXEMPT - found)
    # Assert
    assert not stale, (
        "SCAN_EXEMPT lists files that no longer match the DDL scan: "
        f"{stale}. Delete the entries — an exemption nobody needs is a "
        "blessed coordinate waiting for whatever drifts into its place."
    )


def test_the_exemptions_never_excuse_a_sqlite_importer() -> None:
    """NEGATIVE CONTROL — SCAN_EXEMPT must not swallow a real SQLite user.

    The DDL exemption says "this file only TALKS about tables". If a file that
    actually imports sqlite3 ever landed in it, the DDL gate would go green
    about the one file most likely to be defining a SQLite table. The two
    scans are independent on purpose, and this asserts they stay that way.
    """
    # Arrange
    importers = _all_files_importing_sqlite()
    # Act
    excused = sorted(SCAN_EXEMPT & importers)
    # Assert
    assert not excused, (
        f"SCAN_EXEMPT excuses files that import sqlite3: {excused}. An "
        "exemption justified as 'prose only' cannot cover a module that opens "
        "a database."
    )


# ----------------------------------------------------------------------
# The vendor footprint — SQLite opened without importing sqlite3.
# ----------------------------------------------------------------------
#
# THIS IS THE GATE THAT MUST OUTLIVE THE OTHERS. Once nobody writes
# ``import sqlite3``, a vendor construction is how SQLite returns, and the
# scan above is structurally blind to it. A repo with a clean import scan and
# no vendor scan is precisely the six-months-later state the operator's
# instruction is about.


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
    heuristic excuse the real schema module: a comment describing a thing is
    that thing, to a regex.

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


def test_no_module_opens_sqlite_through_a_vendor_library() -> None:
    """SQLite through a vendor library is still SQLite."""
    # Arrange
    found = _modules_constructing_vendor_sqlite(SRC)
    # Act
    offenders = sorted(found)
    # Assert
    assert not offenders, (
        "NEW VENDORED SQLITE. These modules open a SQLite database through a "
        f"third-party library rather than through sqlite3: {offenders}. No "
        "import check can see this — the sqlite3 import happens inside the "
        "vendor package — which is exactly why it is scanned separately. A "
        "new per-agent SQLite file is the same decision as a new state.db, "
        "whoever writes the connect()."
    )
