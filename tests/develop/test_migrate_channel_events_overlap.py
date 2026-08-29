#!/usr/bin/env python3
"""Two hosts served the SAME target over OVERLAPPING periods. Import both.

THE FALSE REFUSAL THIS PINS (measured on the fleet, 2026-08-28)
===============================================================
The ordering guard refuses a target when the store already holds a row NEWER
than everything in the source ``state.db``, on the reasoning that such a row
cannot belong to an OLDER residency and must therefore be the live daemon
having moved on. That reasoning holds only for SEQUENTIAL relocation.

compute-04 and compute-03 both served ``scitex-agent-container``,
``scitex-cards`` and ``figrecipe``, over INTERLEAVED date ranges. Once
compute-04's history was imported, compute-03's import was refused by rows
that were not daemon traffic at all — 145, 2 and 5 of them, with ZERO written
after the cutover. The remedy the message printed ("stop ``sac listen`` and
re-run") could not work, because stopping a daemon does not remove rows that
are already in the table. The predicate was unsatisfiable.

WHAT MUST STILL FAIL. The tests at the bottom are the NEGATIVE CONTROLS, and
they matter more than the ones above them: a "fix" that simply stopped
counting newer rows would make the whole module green while restoring the
corruption the guard exists to prevent. A row the DAEMON minted in the deploy
gap has no import provenance, and must still refuse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state.state_db_channel_store import (
    reset_channel_connection,
)
from tests.develop._channel_migration_kit import (
    event_row,
    execute,
    legacy_db,
    query,
    run,
)


@pytest.fixture(autouse=True)
def _drop_cached_connection() -> Iterator[None]:
    """Close the process-wide handle around every test in this module."""
    reset_channel_connection()
    yield
    reset_channel_connection()


# ---------------------------------------------------------------------------
# The two residencies. Their timestamps INTERLEAVE — that is the whole point.
#
#   earlier host   1.0        5.0        9.0     (ids 1, 2, 3)
#   later host           3.0        7.0          (ids 1, 2)
#
# Neither range contains the other, and the earlier host's NEWEST row (9.0)
# postdates the later host's newest (7.0). Under the ts-only discriminator
# that makes the later host's import permanently refusable.
# ---------------------------------------------------------------------------


def _earlier_host(tmp_path: Path) -> Path:
    return legacy_db(
        tmp_path / "compute-04",
        [
            event_row("lead", "a-early", 1.0, delivered=1.5),
            event_row("lead", "a-mid", 5.0),
            event_row("lead", "a-late", 9.0),
        ],
    )


def _later_host(tmp_path: Path) -> Path:
    return legacy_db(
        tmp_path / "compute-03",
        [
            event_row("lead", "b-first", 3.0),
            event_row("lead", "b-second", 7.0),
        ],
    )


def test_overlapping_residency_imports_instead_of_refusing(
    tmp_path: Path, pg_schema: str
) -> None:
    """THE BUG. A second host whose history INTERLEAVES the first must import.

    Nothing here is post-cutover traffic: every row in the store came from the
    earlier host's ``state.db``. Refusing this is refusing a migration that
    can never be performed.
    """
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 0


def test_overlapping_residency_preserves_the_first_hosts_ids(
    tmp_path: Path, pg_schema: str
) -> None:
    """The earlier host keeps 1..3; the later host is OFFSET above it.

    Id preservation is the property a live ``Last-Event-ID`` rests on, and the
    fix must not buy the import at its expense.
    """
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    # Act
    run(later, "--commit")
    # Assert — 1..3 are the earlier host's, in its own order; 4..5 the later's.
    assert query(
        "SELECT id, content FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [
        (1, "a-early"),
        (2, "a-mid"),
        (3, "a-late"),
        (4, "b-first"),
        (5, "b-second"),
    ]


def test_overlapping_residency_is_idempotent(tmp_path: Path, pg_schema: str) -> None:
    """Re-running the later host after the fix must still move nothing."""
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    run(later, "--commit")
    # Act
    run(later, "--commit")
    # Assert
    assert query(
        "SELECT COUNT(*) FROM sac_channel_events WHERE target = %s", ("lead",)
    ) == [(5,)]


def test_the_dry_run_stops_refusing_the_overlap_too(
    tmp_path: Path, pg_schema: str
) -> None:
    """The preview and the commit must agree — a dry run that refuses a run
    which would succeed is as misleading as the reverse."""
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    # Act
    rc = run(later)
    # Assert
    assert rc == 0


def test_the_import_window_is_recorded_for_each_host(
    tmp_path: Path, pg_schema: str
) -> None:
    """PROVENANCE IS THE MECHANISM, so it is asserted rather than assumed.

    Without a recorded window the guard has nothing to distinguish an imported
    row from a daemon row, and this whole module would be green only because
    the guard had been weakened.
    """
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    # Act
    run(later, "--commit")
    # Assert
    assert query(
        "SELECT lo_id, hi_id, row_count, offset_applied FROM sac_channel_import "
        "WHERE target = %s ORDER BY lo_id",
        ("lead",),
    ) == [(1, 3, 3, 0), (4, 5, 2, 3)]


def _unattributed_earlier_host(tmp_path: Path) -> tuple[Path, Path]:
    """The state TONIGHT'S STORE is in: history imported, provenance absent.

    The earlier host lands and its ledger row is then removed, which is
    exactly what a store imported by the pre-provenance script looks like —
    7,980 rows nothing can account for.
    """
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    execute("DELETE FROM sac_channel_import WHERE target = %s", ("lead",))
    return earlier, later


def test_history_with_no_recorded_provenance_still_refuses(
    tmp_path: Path, pg_schema: str
) -> None:
    """Un-provenanced rows are treated as the daemon's, not waved through.

    This is the conservative half of the fix and it must stay conservative:
    the script cannot tell, so it does not guess.
    """
    # Arrange
    _earlier, later = _unattributed_earlier_host(tmp_path)
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 1


def test_re_running_the_earlier_host_backfills_its_provenance(
    tmp_path: Path, pg_schema: str
) -> None:
    """THE REMEDY FOR TONIGHT'S STORE — the same command, not a new one.

    Re-running the EARLIER host records the window it already occupies:
    ``_offset_for`` recognises its own rows by content, so the run moves
    nothing and only stamps what it recognised. The later host then imports.
    """
    # Arrange
    earlier, later = _unattributed_earlier_host(tmp_path)
    # Act — the backfill.
    run(earlier, "--commit")
    # Assert
    assert run(later, "--commit") == 0


def test_the_operator_can_accept_one_named_target(
    tmp_path: Path, pg_schema: str
) -> None:
    """The escape hatch when the earlier host's ``state.db`` is GONE.

    Per target, named in full. There is deliberately no ``--force``: a blanket
    bypass restores exactly the corruption the guard exists to prevent, and
    naming the target is what keeps the operator's assertion specific enough
    to be wrong out loud.
    """
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    execute("DELETE FROM sac_channel_import WHERE target = %s", ("lead",))
    # Act
    rc = run(later, "--commit", "--accept-imported-history", "lead")
    # Assert
    assert rc == 0


def test_accepting_a_target_that_is_not_in_the_source_is_an_error(
    tmp_path: Path, pg_schema: str
) -> None:
    """A typo'd target must not silently waive nothing and look like it worked."""
    # Arrange
    earlier = _earlier_host(tmp_path)
    # Act
    refused = pytest.raises(SystemExit)
    # Assert
    with refused:
        run(earlier, "--commit", "--accept-imported-history", "leadd")


# ---------------------------------------------------------------------------
# THE OTHER DIRECTION — a SEQUENTIAL relocation must still take its offset.
#
# Measured on the fleet 2026-08-28: nas-03 holds ``lead`` at ids 1..7 and is
# genuinely the OLDER residency; compute-03 holds ``lead`` at ids 987..989.
# That one SHOULD be shifted above nas-03's rows — it is the design working,
# not a false refusal — and it must not be "fixed" along with the overlap.
# Provenance changes WHEN the guard fires, never WHERE the rows land.
# ---------------------------------------------------------------------------


def _sequential_pair(tmp_path: Path) -> tuple[Path, Path]:
    """nas-03's residency, then compute-03's — disjoint and in order.

    The later host's ids start high (987) because its own SQLite counter had
    been running for months. They carry no meaning in the destination, and
    relocating them is exactly what the offset is for.
    """
    import sqlite3

    earlier = legacy_db(
        tmp_path / "nas-03",
        [event_row("lead", f"nas-{n}", float(n)) for n in range(1, 8)],
    )
    later = legacy_db(
        tmp_path / "compute-03",
        [event_row("lead", f"c03-{n}", 100.0 + n) for n in range(1, 4)],
    )
    # AUTOINCREMENT hands out 1..3; the measured store has 987..989, and the
    # gap between the two hosts' id ranges is half the point of the fixture.
    conn = sqlite3.connect(later)
    conn.execute("UPDATE channel_events SET id = id + 986")
    conn.commit()
    conn.close()
    return earlier, later


def test_a_sequential_relocation_is_still_imported(
    tmp_path: Path, pg_schema: str
) -> None:
    """The genuine relocation is NOT refused — that half was never broken."""
    # Arrange
    earlier, later = _sequential_pair(tmp_path)
    run(earlier, "--commit")
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 0


def test_a_sequential_relocation_is_still_offset_above_the_earlier_host(
    tmp_path: Path, pg_schema: str
) -> None:
    """AND IT STILL MOVES. Provenance must not switch the offset off.

    A change that let the later host keep its own ids would collide with
    nothing here — 987 is free — and would leave the cursor at 989 with seven
    rows stranded below it. The rows belong directly above the earlier
    residency, which is where they have always gone.
    """
    # Arrange
    earlier, later = _sequential_pair(tmp_path)
    run(earlier, "--commit")
    # Act
    run(later, "--commit")
    # Assert — nas-03 keeps 1..7; compute-03's 987..989 land at 994..996.
    assert query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (994,), (995,), (996,)]


def test_the_sequential_offset_is_recorded_as_provenance_too(
    tmp_path: Path, pg_schema: str
) -> None:
    """So a LATER host is not refused by rows this run legitimately placed.

    Without this the fix would only push the false refusal one host along.
    """
    # Arrange
    earlier, later = _sequential_pair(tmp_path)
    run(earlier, "--commit")
    # Act
    run(later, "--commit")
    # Assert
    assert query(
        "SELECT lo_id, hi_id, offset_applied FROM sac_channel_import "
        "WHERE target = %s ORDER BY lo_id",
        ("lead",),
    ) == [(1, 7, 0), (994, 996, 7)]


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — the hazard the guard exists for must STILL be caught,
# and now it has to be caught in the presence of a populated ledger.
# ---------------------------------------------------------------------------


def _daemon_has_served(target: str, *, ts: float) -> None:
    """The REAL writer mints an id, exactly as ``sac listen`` would."""
    from scitex_agent_container._state.state_db_channel import persist_event

    persist_event(target=target, event={"msg_id": "post-cutover", "ts": ts})


def test_a_daemon_row_above_a_recorded_import_still_refuses(
    tmp_path: Path, pg_schema: str
) -> None:
    """THE CONTROL THAT MATTERS MOST.

    The store holds a recorded import AND a row the daemon minted after it.
    Provenance covers the first and not the second, so the refusal must fire
    on the second alone. A fix that merely subtracted "rows that are newer"
    would pass every test above and fail this one.
    """
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    _daemon_has_served("lead", ts=99.0)
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 1


def test_that_refusal_writes_nothing(tmp_path: Path, pg_schema: str) -> None:
    """All-or-nothing survives the change: no partial import, no shifted ids."""
    # Arrange
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    _daemon_has_served("lead", ts=99.0)
    # Act
    run(later, "--commit")
    # Assert — the earlier host's 3 rows plus the daemon's 1, and nothing else.
    assert query(
        "SELECT COUNT(*) FROM sac_channel_events WHERE target = %s", ("lead",)
    ) == [(4,)]


def test_accepting_a_different_target_does_not_waive_this_one(
    tmp_path: Path, pg_schema: str
) -> None:
    """The override is PER TARGET, and the test proves it does not leak.

    ``ci`` is named; ``lead`` is the one with the daemon row. If the flag were
    a disguised ``--force`` this would return 0.
    """
    # Arrange
    earlier = _earlier_host(tmp_path)
    later = legacy_db(
        tmp_path / "compute-03",
        [event_row("lead", "b-first", 3.0), event_row("ci", "c-first", 3.5)],
    )
    run(earlier, "--commit")
    _daemon_has_served("lead", ts=99.0)
    # Act
    rc = run(later, "--commit", "--accept-imported-history", "ci")
    # Assert
    assert rc == 1
