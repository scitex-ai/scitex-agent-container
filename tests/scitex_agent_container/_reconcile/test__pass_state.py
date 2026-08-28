"""``_reconcile._pass`` — the reconciler must be able to prove its OWN state.

Third sibling of ``test__pass.py`` (which agents get restarted) and
``test__pass_limits.py`` (the rate limits). This file owns one question: what
happens when the reconciler cannot read the ONE piece of state it keeps —
the history of which agents it has already auto-restarted.

That history is the only thing enforcing the debounce and the hourly cap, so
a read that fails OPEN (``except OSError: return {}``) makes "forbidden to
read" identical to "nothing restarted yet" — silently disarming both limits
and turning the enforcer into the restart loop it exists to prevent. The
reconciler must therefore not depend on a path it cannot PROVE it can read,
must treat present/missing/denied/unreadable as DISTINCT outcomes, and must
ALARM rather than quietly no-op when it cannot tell.

Measured precedent (Spartan, 2026-07-16): ``~/.scitex`` is a SYMLINK into a
project whose membership was revoked, so every ``$HOME``-resolved path under
it became permission-denied for fresh processes — while everything still
LOOKED installed and configured. The fault the reconciler exists to catch
must not be able to take out the reconciler.

Same no-mocks setup: the denial is ORGANIC (a real read-only directory), not
an injected raiser. Fixtures in ``conftest.py``, helpers in ``_fleet.py``.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io

from scitex_agent_container._events import (
    SELF_IMPAIRED,
    SELF_RECOVERED,
    read_events,
)
from scitex_agent_container._reconcile._alarm import SUBSYSTEM
from scitex_agent_container._reconcile._rule import Verdict
from tests.scitex_agent_container._reconcile._fleet import (
    Recorder,
    ghost,
    run_pass,
    verdict_of,
    write_spec,
)


def _kinds(events) -> list[str]:
    """The self-state events this pass recorded, in order."""
    return [
        e.event
        for e in read_events(events, subsystem=SUBSYSTEM)
        if e.event in (SELF_IMPAIRED, SELF_RECOVERED)
    ]


# --- an unreadable BUDGET halts restarts and ALARMS -------------------------
#
# The reconciler must not depend on a path it cannot prove it can read. Its
# history is the ONLY memory of what it has already restarted, so a denied
# read would silently disarm the debounce AND the hourly cap — every corpse
# restartable on every 5-minute tick, forever. Measured precedent (Spartan,
# 2026-07-16): ~/.scitex is a SYMLINK into a project whose membership was
# revoked, denying every path under it while still LOOKING configured. The
# fault the reconciler exists to catch must not be able to take out the
# reconciler.


def test_unreadable_budget_restarts_nothing(pg_schema: str, registry, db_path, events, tmp_path):
    # Arrange — a corpse, and a history we are forbidden to read.
    write_spec(registry, "alpha")
    ghost("alpha")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    recorder = Recorder()
    try:
        # Act
        run_pass(
      registry,
      
      denied / "hist.json",
            events,
            apply=True,
            restart_fn=recorder,
            err_stream=io.StringIO(),
        )
        # Assert — an unenforceable budget is not a budget.
        assert recorder.names == []
    finally:
        denied.chmod(0o755)


def test_unreadable_budget_is_reported_per_agent(pg_schema: str, registry, db_path, events, tmp_path):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        outcome = run_pass(
      registry,
      
      denied / "hist.json",
            events,
            apply=True,
            err_stream=io.StringIO(),
        )
        # Assert
        assert verdict_of(outcome, "alpha") is Verdict.BUDGET_UNKNOWN
    finally:
        denied.chmod(0o755)


def test_unreadable_budget_records_self_impaired(pg_schema: str, registry, db_path, events, tmp_path):
    # Arrange — a reconciler that quietly does nothing is exactly the
    # "renewal mechanism that cannot report its own failure" class. It must
    # ALARM, not no-op.
    write_spec(registry, "alpha")
    ghost("alpha")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        run_pass(
      registry,
      
      denied / "hist.json",
            events,
            apply=True,
            err_stream=io.StringIO(),
        )
        # Assert
        assert _kinds(events) == [SELF_IMPAIRED]
    finally:
        denied.chmod(0o755)


def test_unreadable_budget_is_loud(pg_schema: str, registry, db_path, events, tmp_path):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    stream = io.StringIO()
    try:
        # Act
        run_pass(
      registry,
      
      denied / "hist.json",
            events,
            apply=True,
            err_stream=stream,
        )
        # Assert
        assert "REFUSING to restart" in stream.getvalue()
    finally:
        denied.chmod(0o755)


def test_unreadable_budget_exits_two(pg_schema: str, registry, db_path, events, tmp_path):
    # Arrange — being blind about our OWN state must not exit 0 and let a
    # cron log it as a healthy tick.
    write_spec(registry, "alpha")
    ghost("alpha")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0o555)
    try:
        # Act
        outcome = run_pass(
      registry,
      
      denied / "hist.json",
            events,
            apply=True,
            err_stream=io.StringIO(),
        )
        # Assert
        assert outcome.exit_code() == 2
    finally:
        denied.chmod(0o755)


def test_corrupt_budget_restarts_nothing(pg_schema: str, registry, db_path, history, events):
    # Arrange — a corrupt history means we HAVE a memory and cannot parse
    # it. Treating that as "nothing restarted" disarms the budget just as
    # thoroughly as a permission error.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text("{corrupt")
    recorder = Recorder()
    # Act
    run_pass(
    registry,
    
    history,
        events,
        apply=True,
        restart_fn=recorder,
        err_stream=io.StringIO(),
    )
    # Assert
    assert recorder.names == []


def test_readable_budget_records_self_recovered(pg_schema: str, registry, db_path, history, events):
    # Arrange — the state was unreadable once, so an impairment is on record.
    write_spec(registry, "alpha")
    ghost("alpha")
    history.write_text("{corrupt")
    run_pass(registry, history, events, apply=True, err_stream=io.StringIO())
    history.unlink()
    # Act — the state is readable again; a fixed problem must say so, once.
    run_pass(registry, history, events, apply=True)
    # Assert
    assert _kinds(events) == [SELF_IMPAIRED, SELF_RECOVERED]


def test_a_healthy_pass_records_no_self_state(pg_schema: str, registry, db_path, history, events):
    # Arrange — a normal first run must not alarm about its own state, AND
    # must not assert its own health either: the pass runs every five minutes
    # forever, so a per-tick "I am fine" would both flood the log and empty
    # ``self-recovered`` of meaning.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(registry, history, events, apply=True)
    # Assert
    assert _kinds(events) == []
