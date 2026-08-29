#!/usr/bin/env python3
"""``--accept-post-cutover-replay``: naming the consequence, not hiding it.

WHY A SECOND WAIVER RATHER THAN REUSING THE FIRST
=================================================
Measured on the primary 2026-08-29, importing compute-03's last host: the
guard refused ``scitex-agent-container`` over 13 rows at ids 7981..7993,
written between 21:53 and 00:10 that night by ``ci`` and
``figrecipe-sqlite-out``, every one ``delivered=True``. They are live daemon
traffic, and the refusal was CORRECT — a true positive, not another false
shape. 233 further newer rows were excused automatically because a recorded
import accounted for them, which is the provenance mechanism working.

But no remedy the message offered could clear it:

* re-importing the other host's ``state.db`` does not apply — these rows came
  from no import;
* ``--accept-imported-history`` would have been a FALSE assertion;
* and "stop ``sac listen`` and re-run", which the message did say, CANNOT
  work: the rows are already written and stopping a daemon does not remove
  rows. That was the same structural flaw as the guard this effort was
  dispatched to fix, reproduced in its own remedy text.

So the honest move is a flag that states what is true — these ARE
post-cutover writes — and makes the operator accept a named, bounded cost.

WHY THE COST IS BOUNDED, measured rather than asserted: ``offset_for``
returns ``pg_max``, so this host's rows land ABOVE the daemon's. The
post-cutover rows keep their ids and stay reachable; nothing is stranded.
The whole harm is that a consumer sitting exactly at the top id receives this
host's rows as if new. That is the OPPOSITE of the catastrophic shape, which
needs the daemon rows to hold LOW ids relative to the imported range.
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
    reset_channel_connection()
    yield
    reset_channel_connection()


def _earlier_host(tmp_path: Path) -> Path:
    return legacy_db(
        tmp_path / "host-a",
        [event_row("lead", "a-1", 1.0), event_row("lead", "a-2", 2.0)],
    )


def _later_host(tmp_path: Path) -> Path:
    return legacy_db(tmp_path / "host-b", [event_row("lead", "b-1", 3.0)])


def _daemon_served(target: str, *, ts: float) -> int:
    """The REAL writer mints an id, exactly as ``sac listen`` would."""
    from scitex_agent_container._state.state_db_channel import persist_event

    return persist_event(target=target, event={"msg_id": f"post-{ts}", "ts": ts})


def _blocked(tmp_path: Path) -> tuple[Path, Path]:
    """The measured shape: an accounted-for import PLUS live daemon rows."""
    earlier, later = _earlier_host(tmp_path), _later_host(tmp_path)
    run(earlier, "--commit")
    _daemon_served("lead", ts=99.0)
    return earlier, later


def test_the_replay_waiver_lets_the_import_through(
    tmp_path: Path, pg_schema: str
) -> None:
    """The blocked target imports once the operator accepts the named cost."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    rc = run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert rc == 0


def test_the_daemon_rows_keep_their_ids(tmp_path: Path, pg_schema: str) -> None:
    """THE CLAIM THE FLAG MAKES. Nothing post-cutover moves or disappears.

    If the waiver shifted the daemon's rows it would be selling the
    catastrophic outcome under a reassuring name.
    """
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert — 1,2 the earlier import; 3 the daemon's; 4 this host's, ABOVE it.
    assert query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,), (3,), (4,)]


def test_the_waiver_prints_the_consequence_it_accepts(
    tmp_path: Path, pg_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A flag whose name is honest and whose output is silent is still a trap.

    The operator has to see the target, the count, the id range and the replay
    risk at the point of use — not go and measure them afterwards, which is
    what this incident actually cost.
    """
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert "Last-Event-ID" in capsys.readouterr().out


def test_the_printed_consequence_names_the_id_range(
    tmp_path: Path, pg_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """"1 row(s) at ids 3..3" is checkable; "some rows" is not."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert "ids 3..3" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# THE TWO WAIVERS MUST NOT COLLAPSE INTO ONE HABIT.
# ---------------------------------------------------------------------------


def test_naming_both_waivers_for_one_target_is_refused(
    tmp_path: Path, pg_schema: str
) -> None:
    """They assert opposite things; at most one can be true."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    rc = run(
        later,
        "--commit",
        "--accept-post-cutover-replay",
        "lead",
        "--accept-imported-history",
        "lead",
    )
    # Assert
    assert rc == 1


def test_a_waiver_for_a_target_that_is_not_blocked_is_refused(
    tmp_path: Path, pg_schema: str
) -> None:
    """An override for a case that already has a mechanism is refused.

    Silently ignoring it is how "pass the flag anyway" becomes the habit that
    a guard cannot survive.
    """
    # Arrange — the earlier host alone; nothing is blocking anything.
    earlier = _earlier_host(tmp_path)
    # Act
    rc = run(earlier, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert rc == 1


def test_that_refusal_says_the_case_has_its_own_mechanism(
    tmp_path: Path, pg_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusing without saying why just moves the confusion."""
    # Arrange
    earlier = _earlier_host(tmp_path)
    # Act
    run(earlier, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert "nothing to waive" in capsys.readouterr().out


def test_the_waiver_is_per_target_and_does_not_leak(
    tmp_path: Path, pg_schema: str
) -> None:
    """``ci`` is named; ``lead`` is the blocked one. A disguised --force
    would return 0."""
    # Arrange
    earlier = _earlier_host(tmp_path)
    later = legacy_db(
        tmp_path / "host-b",
        [event_row("lead", "b-1", 3.0), event_row("ci", "c-1", 3.5)],
    )
    run(earlier, "--commit")
    _daemon_served("lead", ts=99.0)
    # Act
    rc = run(later, "--commit", "--accept-post-cutover-replay", "ci")
    # Assert
    assert rc == 1


def test_a_typoed_target_is_still_refused(tmp_path: Path, pg_schema: str) -> None:
    """A waiver aimed at a target this state.db lacks waives NOTHING, and
    would read exactly like one that worked."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    refused = pytest.raises(SystemExit)
    # Assert
    with refused:
        run(later, "--commit", "--accept-post-cutover-replay", "leadd")


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — the guard must still refuse without the flag.
# ---------------------------------------------------------------------------


def test_without_the_flag_the_target_is_still_refused(
    tmp_path: Path, pg_schema: str
) -> None:
    """The whole point is that this is opt-in per target, every run."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 1


def test_the_remedy_text_no_longer_promises_that_stopping_clears_it(
    tmp_path: Path, pg_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """CONDITION 3. The old text sent an operator to stop the daemon and
    re-run, which cannot work — the rows are already written."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    # Act
    run(later, "--commit")
    # Assert
    assert "STOPPING THE DAEMON WILL NOT CLEAR THIS" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# MOOT IS NOT UNNECESSARY.
#
# Measured 2026-08-29, re-running the exact command that had just completed
# compute-03's import against merged develop:
#
#   --commit --db-path <compute-03> --accept-post-cutover-replay scitex-agent-container
#     REFUSED ... named to a waiver flag, but nothing is blocking it   exit 1
#   --commit --db-path <compute-03>
#     exit 0, 19/19 targets MATCHES SQLite, nothing moved
#
# The refusal was literally true -- after a successful import nothing IS
# blocking the target -- and it still broke the script's own RE-RUNNING IS
# SAFE contract. exit 1 on an already-correct state is indistinguishable from
# real failure to a retry wrapper, a cron, or an operator following a runbook.
#
# The cause was one line's position: `unnecessary.discard(target)` sat below
# both early-continues, so an already-imported target never left the set.
# ---------------------------------------------------------------------------


def test_re_running_a_successful_import_with_its_flag_is_a_no_op(
    tmp_path: Path, pg_schema: str
) -> None:
    """THE REGRESSION. The documented command must stay re-runnable."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Act
    rc = run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert rc == 0


def test_that_re_run_moves_nothing(tmp_path: Path, pg_schema: str) -> None:
    """Idempotent in the rows too, not merely in the exit code."""
    # Arrange
    _earlier, later = _blocked(tmp_path)
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Act
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,), (3,), (4,)]


def test_that_re_run_says_the_flag_was_moot(
    tmp_path: Path, pg_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently accepting it would teach the operator the flag is always fine.

    Saying "already imported, the flag did nothing" is what keeps the next
    person from concluding the waiver is a harmless thing to leave on.
    """
    # Arrange
    _earlier, later = _blocked(tmp_path)
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    capsys.readouterr()
    # Act
    run(later, "--commit", "--accept-post-cutover-replay", "lead")
    # Assert
    assert "ALREADY IMPORTED" in capsys.readouterr().out


def _overlapping_unattributed(tmp_path: Path) -> tuple[Path, Path]:
    """An earlier host that OVERLAPS the later one, with its ledger removed.

    The earlier host's 9.0 row postdates the later host's newest (3.0), so it
    blocks; deleting the ledger entry makes it unattributed, which is the
    state a store imported before provenance existed is in. Without the
    overlap there is nothing to waive and the FIRST run refuses too — which
    is what my initial version of this test got wrong.
    """
    earlier = legacy_db(
        tmp_path / "host-a",
        [event_row("lead", "a-1", 1.0), event_row("lead", "a-late", 9.0)],
    )
    later = _later_host(tmp_path)
    run(earlier, "--commit")
    execute("DELETE FROM sac_channel_import WHERE target = %s", ("lead",))
    return earlier, later


def test_the_imported_history_waiver_is_needed_first(
    tmp_path: Path, pg_schema: str
) -> None:
    """CONTROL for the test below: without the flag this really is blocked.

    A re-run test proves nothing if the first run never needed the flag.
    """
    # Arrange
    _earlier, later = _overlapping_unattributed(tmp_path)
    # Act
    rc = run(later, "--commit")
    # Assert
    assert rc == 1


def test_the_same_holds_for_the_imported_history_waiver(
    tmp_path: Path, pg_schema: str
) -> None:
    """Both flags share the code path, so both share the contract."""
    # Arrange
    _earlier, later = _overlapping_unattributed(tmp_path)
    run(later, "--commit", "--accept-imported-history", "lead")
    # Act
    rc = run(later, "--commit", "--accept-imported-history", "lead")
    # Assert
    assert rc == 0
