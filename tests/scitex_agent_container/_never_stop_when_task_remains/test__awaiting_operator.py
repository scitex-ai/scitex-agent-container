"""Tests for the awaiting-operator REPORT.

PA-306 no-mocks. The read command is a REAL executable on disk, spawned by a
real :func:`subprocess.run`, printing real JSON and exiting with a real code —
the same interface ``scitex-cards list-tasks --json`` presents. The cache is
real files under a real (redirected) runtime tree.

THE FAILURE UNDER TEST is not "does it print a number". It is that a card with
``status=blocked`` is counted by nothing — no nudge, no runnable-items line —
so it stops existing, and an agent reporting "board clear" is telling the
truth about the only number it can see. Measured 2026-08-11: 21 such cards on
this agent's board, 24 on scitex-dev's, oldest three weeks old.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

from scitex_agent_container._never_stop_when_task_remains._awaiting_operator import (
    CMD_ENV,
    TTL_ENV,
    cache_path,
    notice,
    query_argv,
    render,
    summarize,
)

from ._fake_detector import (
    SCOPE_ENV,
    awaiting_cards,
    isolate_runtime,
    operator_card,
    scope_sensitive_board,
    unreadable_board,
)

_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _card(card_id: str, *, days: int, blocker: str = "operator-decision") -> dict:
    stamp = _NOW - timedelta(days=days)
    return {
        "id": card_id,
        "status": "blocked",
        "blocker": blocker,
        "blocked_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# what counts as waiting on a human
# ---------------------------------------------------------------------------


def test_operator_decision_cards_are_counted():
    # Arrange
    rows = [_card("q-1", days=3), _card("q-2", days=1)]
    # Act
    count, _ = summarize(rows, now=_NOW)
    # Assert
    assert count == 2


def test_agent_wait_is_not_counted_as_awaiting_the_operator():
    """An agent waiting on another agent is a DIFFERENT failure with a
    different owner. Counting it here would misattribute the gate and dilute
    the one number this line exists to make unignorable."""
    # Arrange
    rows = [_card("q-1", days=3), _card("peer-1", days=9, blocker="agent-wait")]
    # Act
    count, _ = summarize(rows, now=_NOW)
    # Assert
    assert count == 1


def test_a_card_that_is_no_longer_blocked_is_not_waiting():
    # Arrange
    row = _card("q-1", days=3)
    row["status"] = "done"
    # Act
    count, _ = summarize([row], now=_NOW)
    # Assert
    assert count == 0


def test_the_oldest_age_is_reported_in_days():
    """The AGE is the part that does the work — a count alone reads as steady
    state, "oldest 24 days" reads as a problem."""
    # Arrange
    rows = [_card("q-1", days=3), _card("q-2", days=24), _card("q-3", days=1)]
    # Act
    _, oldest = summarize(rows, now=_NOW)
    # Assert
    assert oldest == 24


def test_the_age_ignores_cards_that_are_not_waiting():
    """A 90-day agent-wait card must not inflate the operator's number."""
    # Arrange
    rows = [_card("q-1", days=3), _card("peer", days=90, blocker="agent-wait")]
    # Act
    _, oldest = summarize(rows, now=_NOW)
    # Assert
    assert oldest == 3


def test_created_at_stands_in_when_the_store_recorded_no_blocked_at():
    """Measured on the live board: only 15 of 21 rows carry ``blocked_at``.
    Dropping the other 6 would silently understate the queue."""
    # Arrange
    row = _card("q-1", days=0)
    row.pop("blocked_at")
    row["created_at"] = (_NOW - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Act
    count, oldest = summarize([row], now=_NOW)
    # Assert
    assert (count, oldest) == (1, 31)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_the_line_reports_the_count():
    # Arrange
    count, oldest = 21, 24
    # Act
    line = render(count, oldest)
    # Assert
    assert "21 card(s) awaiting the operator" in line


def test_the_line_reports_the_age_of_the_oldest():
    # Arrange
    count, oldest = 21, 24
    # Act
    line = render(count, oldest)
    # Assert
    assert "oldest 24 days" in line


def test_the_line_says_what_to_do_about_it():
    # Arrange
    count, oldest = 21, 24
    # Act
    line = render(count, oldest)
    # Assert
    assert "surface or reclassify" in line


def test_an_empty_queue_prints_nothing():
    """No queue, no line — a report that fires on a clean board is noise, and
    noise is how a hook earns its way into being disabled."""
    # Arrange
    count, oldest = 0, None
    # Act
    line = render(count, oldest)
    # Assert
    assert line == ""


def test_a_countable_queue_with_no_readable_stamp_still_reports_the_count():
    """An invented age would be worse than no age; the count still stands."""
    # Arrange
    count, oldest = 4, None
    # Act
    line = render(count, oldest)
    # Assert
    assert line == "⏸ 4 card(s) awaiting the operator — surface or reclassify"


def test_one_day_is_not_pluralised():
    # Arrange
    count, oldest = 1, 1
    # Act
    line = render(count, oldest)
    # Assert
    assert "oldest 1 day)" in line


# ---------------------------------------------------------------------------
# the read command
# ---------------------------------------------------------------------------


def test_the_query_names_the_agent():
    """Scoped to ONE board on purpose — "surface or reclassify" is only advice
    you can follow about your own cards."""
    # Arrange
    agent = "agent-x"
    # Act
    argv = query_argv(agent)
    # Assert
    assert argv[argv.index("--assignee") + 1] == "agent-x"


def test_the_query_asks_only_for_blocked_cards():
    # Arrange
    agent = "agent-x"
    # Act
    argv = query_argv(agent)
    # Assert
    assert argv[argv.index("--status") + 1] == "blocked"


def test_the_query_asks_only_for_the_operator_decision_blocker():
    """status=blocked AND blocker=operator-decision — spelled in flags far
    older than ``--blocking-me``, because the fleet always runs older than the
    published version and a report nobody's host can run is not a fix."""
    # Arrange
    agent = "agent-x"
    # Act
    argv = query_argv(agent)
    # Assert
    assert argv[argv.index("--blocker") + 1] == "operator-decision"


def test_the_query_opts_out_of_the_ambient_scope():
    """MEASURED 2026-08-12 on the live board: ``list-tasks`` silently ANDs
    ``$SCITEX_TODO_SCOPE`` into the filter, so with it set the same query
    returned 0 rows from a board holding 21. A report that an ambient env var
    can silence to zero reproduces the exact defect it exists to fix."""
    # Arrange
    agent = "agent-x"
    # Act
    argv = query_argv(agent)
    # Assert
    assert argv[argv.index("--scope") + 1] == ""


def test_the_query_asks_for_machine_readable_output():
    # Arrange
    agent = "agent-x"
    # Act
    argv = query_argv(agent)
    # Assert
    assert "--json" in argv


# ---------------------------------------------------------------------------
# end to end through a real subprocess
# ---------------------------------------------------------------------------


def test_a_real_reader_produces_the_line(env_save_restore, tmp_path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    awaiting_cards(
        env_save_restore,
        tmp_path,
        [operator_card(f"q-{n}", blocked_days_ago=n + 1) for n in range(21)],
    )
    # Act
    line = notice("agent-x")
    # Assert
    assert "21 card(s) awaiting the operator (oldest 21 days)" in line


# ---------------------------------------------------------------------------
# AN AMBIENT ENV VAR MUST NOT BE ABLE TO SILENCE THE ALARM
#
# `list-tasks` silently ANDs $SCITEX_TODO_SCOPE into its filter. Measured on
# the live board 2026-08-12: the same query returned 21 rows unset and 0 rows
# with the variable set. A new alarm that quietly reports zero is WORSE than no
# alarm — it converts "nobody looked" into "we checked and it was clear", which
# is the exact failure family this feature exists to remove. Testing only the
# unset case would have passed, and shipped it.
# ---------------------------------------------------------------------------


def test_the_reader_really_is_silenced_by_an_ambient_scope(
    env_save_restore, tmp_path
):
    """THE CONTROL. Without it the test below proves nothing: a reader that
    ignored scope would pass whether or not the fix were present."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    script = scope_sensitive_board(
        env_save_restore,
        tmp_path,
        [operator_card(f"q-{n}", blocked_days_ago=n + 1) for n in range(21)],
    )
    env_save_restore.set(SCOPE_ENV, "agent:somebody-else")
    # Act — the pre-fix argv: no --scope of our own
    proc = subprocess.run(
        [str(script), "--assignee", "agent-x", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Assert
    assert json.loads(proc.stdout) == []


def test_an_ambient_scope_cannot_silence_the_report(env_save_restore, tmp_path):
    """21 cards on the board AND a narrow ambient scope: still 21."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    scope_sensitive_board(
        env_save_restore,
        tmp_path,
        [operator_card(f"q-{n}", blocked_days_ago=n + 1) for n in range(21)],
    )
    env_save_restore.set(SCOPE_ENV, "agent:somebody-else")
    # Act
    line = notice("agent-x")
    # Assert
    assert "21 card(s) awaiting the operator" in line


def test_the_report_is_unchanged_when_no_scope_is_set(env_save_restore, tmp_path):
    """The opt-out must be a no-op in the ordinary case, not a second filter."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    scope_sensitive_board(
        env_save_restore,
        tmp_path,
        [operator_card(f"q-{n}", blocked_days_ago=n + 1) for n in range(21)],
    )
    env_save_restore.delete(SCOPE_ENV)
    # Act
    line = notice("agent-x")
    # Assert
    assert "21 card(s) awaiting the operator" in line


# ---------------------------------------------------------------------------
# fail open and SILENT — a hook that breaks the stop path breaks everything
# ---------------------------------------------------------------------------


def test_a_refused_read_produces_no_line(env_save_restore, tmp_path):
    """The database was refusing reads intermittently on the night this was
    written. Degrading to today's behaviour is the requirement."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    unreadable_board(env_save_restore, tmp_path)
    # Act
    line = notice("agent-x")
    # Assert
    assert line == ""


def test_output_that_is_not_json_produces_no_line(env_save_restore, tmp_path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    awaiting_cards(
        env_save_restore, tmp_path, stdout="Error: No such option '--blocker'"
    )
    # Act
    line = notice("agent-x")
    # Assert
    assert line == ""


def test_a_missing_reader_produces_no_line(env_save_restore, tmp_path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    env_save_restore.set(CMD_ENV, str(tmp_path / "no-such" / "reader"))
    env_save_restore.set(TTL_ENV, "0")
    # Act
    line = notice("agent-x")
    # Assert
    assert line == ""


def test_an_unresolved_identity_produces_no_line(env_save_restore, tmp_path):
    """Never guess whose board to read — the same rule as ``_identity``."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    awaiting_cards(env_save_restore, tmp_path, [operator_card("q-1", blocked_days_ago=9)])
    # Act
    line = notice("")
    # Assert
    assert line == ""


def test_an_unwritable_cache_still_produces_the_line(env_save_restore, tmp_path):
    """Losing the cache costs latency; it must never cost the report."""
    # Arrange
    blocker_file = tmp_path / "not-a-dir"
    blocker_file.write_text("")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(blocker_file))
    awaiting_cards(env_save_restore, tmp_path, [operator_card("q-1", blocked_days_ago=9)])
    # Act
    line = notice("agent-x")
    # Assert
    assert "1 card(s) awaiting the operator" in line


# ---------------------------------------------------------------------------
# cost — this runs on EVERY stop attempt
# ---------------------------------------------------------------------------


def test_a_second_stop_within_the_ttl_does_not_respawn_the_reader(
    env_save_restore, tmp_path, subprocess_shim
):
    """A hook that adds latency to every turn gets disabled, and a disabled
    hook protects nothing."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    env_save_restore.delete(CMD_ENV)
    env_save_restore.set(TTL_ENV, "900")
    subprocess_shim.install(
        "scitex-cards",
        stdout=json.dumps([operator_card("q-1", blocked_days_ago=30)]),
    )
    # Act
    first = notice("agent-x")
    second = notice("agent-x")
    # Assert
    assert (first, subprocess_shim.call_count("scitex-cards")) == (second, 1)


def test_an_unreadable_board_is_paid_once_per_ttl_not_once_per_stop(
    env_save_restore, tmp_path, subprocess_shim
):
    """The negative cache IS the fail-open budget: a database that is down
    would otherwise charge the full timeout to every single stop attempt."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    env_save_restore.delete(CMD_ENV)
    env_save_restore.set(TTL_ENV, "900")
    subprocess_shim.install("scitex-cards", exit=1, stderr="ExportRefused: ...")
    # Act
    notice("agent-x")
    notice("agent-x")
    # Assert
    assert subprocess_shim.call_count("scitex-cards") == 1


def test_the_reading_is_cached_per_agent(env_save_restore, tmp_path):
    """One agent's clean board must never be served as another's."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    # Act
    paths = {cache_path("agent-x"), cache_path("agent-y")}
    # Assert
    assert len(paths) == 2
