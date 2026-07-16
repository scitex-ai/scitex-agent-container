"""Tests for ``_reconcile._pass`` — WHICH agents does a real pass restart?

The rate limits, restart failures, exit codes and board rails live in the
sibling ``test__pass_limits.py`` (512-line cap); this file owns the core
question: given a real fleet on disk, who gets restarted and who does not.

No mocks. Real on-disk v3 ``spec.yaml`` files in a tmp fleet registry, a
REAL temp ``state.db`` with REAL ``instances`` rows written by the
production writer, a real temp scitex-todo store, and a real injected clock.
Fixtures live in ``conftest.py``, helpers in ``_fleet.py``.

Two seams, both real callables rather than mocks:

* ``snapshot_fn`` — returns the same ``{session: activity}`` dict the real
  batched ``tmux list-sessions`` probe returns (``None`` = could not look).
  This is the production seam ``_verdict_tmux`` already exposes.
* ``restart_fn`` — a RECORDER. The restart is the one irreversible act, so
  the tests that matter most here are the ones asserting it was NOT called.

THE HEADLINE TEST is ``test_ghost_active_row_agent_is_restarted``: a spec
asking to be kept running, no tmux session, and a row still claiming ACTIVE.
That is precisely the state an OAuth rotation left 33 agents in, and it
asserts this command would have brought them back.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._reconcile._budget import load_history
from scitex_agent_container._reconcile._rule import Verdict
from tests.scitex_agent_container._reconcile._fleet import (
    Recorder,
    detail_of,
    ended,
    ghost,
    run_pass,
    sessions,
    verdict_of,
    write_spec,
)

scitex_todo = pytest.importorskip("scitex_todo")


# --- a live session is left alone ------------------------------------------


def test_agent_with_live_session_is_not_restarted(registry, db_path, history, store):
    # Arrange — the session EXISTS, so the agent is alive.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        store,
        apply=True,
        snapshot_fn=lambda **_: sessions("alpha"),
        restart_fn=recorder,
    )
    # Assert — a live agent is NEVER touched; that would destroy context.
    assert recorder.names == []


def test_agent_with_live_session_reports_ok(registry, db_path, history, store):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(
        registry, db_path, history, store, snapshot_fn=lambda **_: sessions("alpha")
    )
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.OK


def test_live_session_outranks_a_crashed_row(registry, db_path, history, store):
    # Arrange — the row says crashed but tmux says the session is THERE.
    # tmux is the fact; the registry's view is a hypothesis.
    write_spec(registry, "alpha")
    ended("alpha", "crashed")
    recorder = Recorder()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        store,
        apply=True,
        snapshot_fn=lambda **_: sessions("alpha"),
        restart_fn=recorder,
    )
    # Assert
    assert recorder.names == []


# --- THE HEADLINE: tonight's 33 dead agents --------------------------------


def test_ghost_active_row_agent_is_restarted(registry, db_path, history, store):
    # Arrange — EXACTLY tonight's state: spec says keep-running, tmux has no
    # session, and the instances row still claims ended_at IS NULL. Nothing
    # recorded an end, so nobody ended it — it died with the OAuth rotation.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert — this is the fleet coming back.
    assert recorder.names == ["alpha"]


def test_ghost_active_row_is_reported_restarted(registry, db_path, history, store):
    # Arrange
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(registry, db_path, history, store, apply=True)
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.RESTARTED


def test_whole_dead_fleet_is_recovered(registry, db_path, history, store):
    # Arrange — the real shape of the incident: many agents, all ghosts.
    for name in ("a1", "a2", "a3", "a4", "a5"):
        write_spec(registry, name)
        ghost(name)
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert — every one of them comes back in a single pass.
    assert sorted(recorder.names) == ["a1", "a2", "a3", "a4", "a5"]


def test_crashed_agent_is_restarted(registry, db_path, history, store):
    # Arrange — the reaper recorded 'crashed': it died without being asked.
    write_spec(registry, "alpha")
    ended("alpha", "crashed")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == ["alpha"]


def test_reboot_swept_agent_is_restarted(registry, db_path, history, store):
    # Arrange — the host rebooted under it. Nobody chose to stop this agent.
    write_spec(registry, "alpha")
    ended("alpha", "reboot-swept")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == ["alpha"]


def test_always_policy_is_enforced(registry, db_path, history, store):
    # Arrange — policy: always is the other managed policy.
    write_spec(registry, "alpha", policy="always")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == ["alpha"]


# --- THE OPERATOR'S INTENT IS SACRED ---------------------------------------


def test_deliberately_stopped_agent_is_not_restarted(registry, db_path, history, store):
    # Arrange — `sac agents stop` recorded 'stopped'. The operator MEANT it.
    write_spec(registry, "alpha")
    ended("alpha", "stopped")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert — an enforcer that undoes a stop is one nobody can turn off.
    assert recorder.names == []


def test_deliberately_stopped_agent_reports_skipped(registry, db_path, history, store):
    # Arrange
    write_spec(registry, "alpha")
    ended("alpha", "stopped")
    # Act
    outcome = run_pass(registry, db_path, history, store, apply=True)
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.SKIPPED


def test_stopped_agent_skip_states_its_reason(registry, db_path, history, store):
    # Arrange — never silent: the report must SAY why it stood down.
    write_spec(registry, "alpha")
    ended("alpha", "stopped")
    # Act
    outcome = run_pass(registry, db_path, history, store, apply=True)
    # Assert
    assert "DELIBERATELY" in detail_of(outcome, "alpha")


def test_deleted_agent_is_not_restarted(registry, db_path, history, store):
    # Arrange
    write_spec(registry, "alpha")
    ended("alpha", "deleted")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


def test_stopped_agent_raises_no_card(registry, db_path, history, store):
    # Arrange — a stopped agent is a CORRECT state, not a problem. Carding
    # it would train the operator to ignore the board.
    write_spec(registry, "alpha")
    ended("alpha", "stopped")
    # Act
    run_pass(registry, db_path, history, store, apply=True)
    # Assert
    assert scitex_todo.list_tasks(store, blocking_me=True) == []


def test_unmanaged_spec_is_not_restarted(registry, db_path, history, store):
    # Arrange — policy=never (also the DEFAULT for a spec with no restart
    # block). sac never promised to keep this one running.
    write_spec(registry, "alpha", policy="never")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


def test_never_started_spec_is_not_started(registry, db_path, history, store):
    # Arrange — a spec with NO instances row. Starting it would be a start
    # nobody asked for; doing that for every unstarted spec is a storm.
    write_spec(registry, "alpha")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


def test_remote_agent_is_not_restarted_locally(registry, db_path, history, store):
    # Arrange — the row belongs to another host, so its tmux is not ours to
    # read and a local restart would DUPLICATE a live remote agent.
    from scitex_agent_container._state import state_db

    write_spec(registry, "alpha")
    state_db.record_instance_start(name="alpha", host="host-b", pid=1, remote=True)
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert
    assert recorder.names == []


# --- --dry-run mutates NOTHING ---------------------------------------------


def test_dry_run_restarts_nothing(registry, db_path, history, store):
    # Arrange — a corpse that WOULD be restarted under --apply.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act — apply defaults to False.
    run_pass(registry, db_path, history, store, restart_fn=recorder)
    # Assert — the irreversible act was never invoked.
    assert recorder.names == []


def test_dry_run_reports_would_restart(registry, db_path, history, store):
    # Arrange — reporting the corpse is the whole point of the dry run.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(registry, db_path, history, store)
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.WOULD_RESTART


def test_dry_run_records_no_restart(registry, db_path, history, store):
    # Arrange — a dry run must not spend budget it never used, or the next
    # --apply would think alpha was already bounced and debounce it.
    #
    # It asserts the history is EMPTY, not that the file is absent: a first
    # run proves it can CREATE the history (by writing the empty one) rather
    # than assuming, because `FileNotFoundError` cannot distinguish "never
    # written" from "the tree is gone/revoked". Writing our own empty scratch
    # file is not a mutation OF THE FLEET, and a dry-run that skipped the
    # proof would report WOULD-RESTART where --apply would find its budget
    # unreadable and refuse — i.e. it would lie about what --apply does.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(registry, db_path, history, store)
    # Assert
    assert load_history(history) == {}


def test_dry_run_raises_no_down_card(registry, db_path, history, store):
    # Arrange — a dry run reports; it must not mutate the shared board about
    # an AGENT (its own heartbeat is a different, deliberate rail).
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    run_pass(registry, db_path, history, store)
    # Assert
    assert scitex_todo.list_tasks(store, blocking_me=True) == []


# --- "I could not look" is not "nothing is there" --------------------------


def test_blind_probe_restarts_nothing(registry, db_path, history, store):
    # Arrange — snapshot_fn returns None: the probe could not look (tmux
    # wedged, or we are in a container). Inferring death here would restart
    # the ENTIRE fleet at once.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        store,
        apply=True,
        snapshot_fn=lambda **_: None,
        restart_fn=recorder,
    )
    # Assert
    assert recorder.names == []


def test_blind_probe_reports_unknown(registry, db_path, history, store):
    # Arrange — UNKNOWN must never be rendered as clean.
    write_spec(registry, "alpha")
    ghost("alpha")
    # Act
    outcome = run_pass(
        registry, db_path, history, store, apply=True, snapshot_fn=lambda **_: None
    )
    # Assert
    assert verdict_of(outcome, "alpha") is Verdict.UNKNOWN


def test_empty_fleet_seen_from_a_container_is_blindness(
    registry, db_path, history, store
):
    # Arrange — THE TRAP: inside a SIF the probe does not fail, it SUCCEEDS
    # and reports an EMPTY fleet (that container's own /tmp has no tmux
    # server). A binary present/absent read would call all 93 agents dead.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(
        registry,
        db_path,
        history,
        store,
        apply=True,
        snapshot_fn=lambda **_: {},
        in_sif_fn=lambda: True,
        restart_fn=recorder,
    )
    # Assert
    assert recorder.names == []


def test_raising_probe_is_unknown_not_dead(registry, db_path, history, store):
    # Arrange — a probe that EXPLODES is a probe that did not look.
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()

    def _boom(**_):
        raise OSError("tmux server is wedged")

    # Act
    run_pass(
        registry,
        db_path,
        history,
        store,
        apply=True,
        snapshot_fn=_boom,
        restart_fn=recorder,
    )
    # Assert
    assert recorder.names == []


def test_tmux_is_probed_once_per_pass(registry, db_path, history, store):
    # Arrange — probing per agent costs ~93 tmux spawns a tick, the exact
    # O(N)-subprocess cost that blew the heartbeat tick's budget and got it
    # abandoned. The real probe is batched; this pass must use it that way.
    calls: list[int] = []

    def _counting(**_):
        calls.append(1)
        return {}

    for name in ("a1", "a2", "a3", "a4", "a5"):
        write_spec(registry, name)
        ghost(name)
    # Act
    run_pass(registry, db_path, history, store, snapshot_fn=_counting)
    # Assert
    assert len(calls) == 1


# --- one bad spec must not strand the fleet --------------------------------


def test_unreadable_spec_does_not_abort_the_sweep(registry, db_path, history, store):
    # Arrange — one malformed spec must not strand the rest of the fleet.
    (registry / "broken").mkdir()
    (registry / "broken" / "spec.yaml").write_text("{[not: valid: yaml")
    write_spec(registry, "alpha")
    ghost("alpha")
    recorder = Recorder()
    # Act
    run_pass(registry, db_path, history, store, apply=True, restart_fn=recorder)
    # Assert — alpha is still recovered.
    assert recorder.names == ["alpha"]


def test_unreadable_spec_is_reported_unknown(registry, db_path, history, store):
    # Arrange — we cannot know whether it should be running, so we must not
    # guess: it is UNKNOWN, never a corpse.
    (registry / "broken").mkdir()
    (registry / "broken" / "spec.yaml").write_text("{[not: valid: yaml")
    # Act
    outcome = run_pass(registry, db_path, history, store, apply=True)
    # Assert
    assert verdict_of(outcome, "broken") is Verdict.UNKNOWN


def test_scaffold_dirs_are_not_agents(registry, db_path, history, store):
    # Arrange — `_shared` / `_template_*` / `_archive` are scaffolding.
    write_spec(registry, "_template_python")
    # Act
    outcome = run_pass(registry, db_path, history, store)
    # Assert
    assert outcome.reports == ()


def test_missing_registry_is_not_a_crash(registry, db_path, history, store, tmp_path):
    # Arrange — a host with no fleet registry at all reports nothing rather
    # than taking the scheduled pass down.
    # Act
    outcome = run_pass(tmp_path / "nope", db_path, history, store)
    # Assert
    assert outcome.reports == ()
