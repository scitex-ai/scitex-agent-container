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
It does not say the 13 modules below are correct, or that they should stay.
They are the measured footprint on 2026-08-19, recorded so the migration has
a definite scope instead of an estimate. Every one of them is per-host state
under ~/.scitex/agent-container/runtime/state.db; the fleet-shared store
(scitex-cards) is already on PostgreSQL at 127.0.0.1:55432.

It also does not police test code or third-party reads. A test using
`:memory:` creates nothing durable, and reading someone else's .db file is
not sac choosing a storage engine.
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
        "_authheal/_specimen.py",
        "_lifecycle/_rename_db.py",
        "_lifecycle/_rename_plan.py",
        "_state/auth_state.py",
        "_state/dispatch_ledger.py",
        "_state/inbound_ledger.py",
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
