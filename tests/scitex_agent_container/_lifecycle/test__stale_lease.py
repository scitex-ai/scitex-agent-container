"""Stale ``instances`` lease cleanup on the start path.

The operator's failure mode: a previous container died WITHOUT going
through ``agent_stop``, leaving an active ``instances`` row whose
recorded PID is dead. Before this helper, the operator had to run::

    # (the retired per-agent database)
        "DELETE FROM instances WHERE name='<name>' AND ended_at IS NULL"

…otherwise the next ``sac agents start`` no-op'd on the zombie lease.

These tests use a real isolated store (per-test, via the
``SCITEX_AGENT_CONTAINER_STATE_DB`` env override) and a real PID
oracle (the test process's own ``os.getpid()`` for the "alive" case;
a known-dead PID we just forked-and-waited for the "dead" case). No
``unittest.mock`` / ``MagicMock`` / ``monkeypatch`` anywhere — STX-TQ002
+ STX-TQ007 + one assertion per test.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest


class FakeThread:
    """Hand-rolled stand-in for ``threading.Thread`` that NEVER runs.

    This file's spec enables ``health``, so the start path spawns the
    health monitor. With the real ``threading.Thread`` that daemon thread
    OUTLIVES the test and, ~90 s later, writes a birth certificate and logs
    an ERROR into whatever test is running then (develop red, 2026-08-24;
    measured with a capture-immune probe). Same shape as ``FakeThread`` in
    ``test_lifecycle.py``. PA-306: a hand-written stand-in, not a mock.
    """

    def __init__(self, *, target=None, args=(), daemon=False, **_kw) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True

@pytest.fixture
def db_path(tmp_path: Path, pg_schema: str) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (save/restore).

    ``instances`` MOVED to the shared PostgreSQL store on 2026-08-28, so this
    fixture now takes ``pg_schema`` as well: the lease helper's
    ``list_active_instances`` / ``record_instance_stop`` writes land there,
    not in the temp file. The local half is kept because the start path this
    file drives still opens state.db for its other tables — and because a
    temp path is what keeps THIS test's rows out of the host's real database.
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _spawn_dead_pid() -> int:
    """Fork → exit immediately → reap → return the now-dead pid.

    A reaped child PID is reliably dead (``os.kill(pid, 0)`` raises
    ``ProcessLookupError``) on Linux until the kernel recycles it,
    which it will not do within the test's lifetime on any normal
    system. Real OS, no mocks.
    """
    pid = os.fork()
    if pid == 0:  # child — exit immediately
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


# ---------------------------------------------------------------------------
# Stale row + dead pid → cleared
#
# Split into one-assertion tests so CI red names exactly which half of
# the contract broke (the count, the absence of active rows, the
# audit-trail exit_reason, the ended_at timestamp).
# ---------------------------------------------------------------------------


def _drive_clear_dead_pid_scenario(name: str) -> tuple[int, list[dict], dict]:
    """Arrange + Act helper: write an active instances row with a dead
    PID, call the cleaner, then snapshot the post-call state.

    Returns ``(cleared, active_for_name, raw_row)`` so per-fact tests
    can assert exactly one observation each.
    """
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        last_known_instance,
        list_active_instances,
        record_instance_start,
    )

    dead_pid = _spawn_dead_pid()
    record_instance_start(name=name, host="h", pid=dead_pid)
    cleared = clear_stale_instance_lease(name)
    active = [r for r in list_active_instances() if r["name"] == name]
    # Read the tombstone back through the PRODUCTION reader rather than with
    # a raw SELECT: since 2026-08-28 there is no ``instances`` table to
    # SELECT from, and reading through the reader is the stronger check
    # anyway — a write the store accepted but cannot serve would be caught.
    raw = last_known_instance(name)
    return cleared, active, raw


def test_clear_stale_instance_lease_reports_one_row_cleared_on_dead_pid(
    db_path: Path,
) -> None:
    # Arrange
    name = "dead-count"
    # Act
    cleared, _active, _raw = _drive_clear_dead_pid_scenario(name)
    # Assert
    assert cleared == 1


def test_clear_stale_instance_lease_leaves_no_active_row_after_dead_pid_clear(
    db_path: Path,
) -> None:
    # Arrange
    name = "dead-active"
    # Act
    _cleared, active, _raw = _drive_clear_dead_pid_scenario(name)
    # Assert
    assert active == []


def test_clear_stale_instance_lease_sets_exit_reason_to_stale_cleared(
    db_path: Path,
) -> None:
    # Arrange
    name = "audit-reason"
    # Act
    _cleared, _active, raw = _drive_clear_dead_pid_scenario(name)
    # Assert
    assert raw["exit_reason"] == "stale-cleared"


def test_clear_stale_instance_lease_writes_ended_at_when_dead_pid_cleared(
    db_path: Path,
) -> None:
    # Arrange
    name = "audit-ended-at"
    # Act
    _cleared, _active, raw = _drive_clear_dead_pid_scenario(name)
    # Assert
    assert raw["ended_at"] is not None


# ---------------------------------------------------------------------------
# Live row + live pid → preserved (no false clear)
# ---------------------------------------------------------------------------


def _drive_live_pid_scenario(name: str) -> tuple[int, list[dict]]:
    """Arrange + Act helper: write a row pointing at our own
    (guaranteed-alive) PID, call the cleaner, return the count + the
    post-call active rows for ``name``.
    """
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    record_instance_start(name=name, host="h", pid=os.getpid())
    cleared = clear_stale_instance_lease(name)
    active = [r for r in list_active_instances() if r["name"] == name]
    return cleared, active


def test_clear_stale_instance_lease_reports_zero_clears_on_live_pid(
    db_path: Path,
) -> None:
    # Arrange
    name = "live-count"
    # Act
    cleared, _active = _drive_live_pid_scenario(name)
    # Assert
    assert cleared == 0


def test_clear_stale_instance_lease_keeps_live_row_active(db_path: Path) -> None:
    # Arrange
    name = "live-active"
    # Act
    _cleared, active = _drive_live_pid_scenario(name)
    # Assert
    assert len(active) == 1


def test_clear_stale_instance_lease_leaves_ended_at_unset_for_live_row(
    db_path: Path,
) -> None:
    # Arrange
    name = "live-ended-at"
    # Act
    _cleared, active = _drive_live_pid_scenario(name)
    # Assert
    assert active[0]["ended_at"] is None


# ---------------------------------------------------------------------------
# Selectivity: other agents' stale rows must not be cleared
# ---------------------------------------------------------------------------


def _drive_name_scoped_scenario() -> tuple[int, set[str]]:
    """Arrange + Act helper: two agents, both stale; clear only
    ``target``. Return the cleared-count plus the post-call set of
    still-active names so per-fact tests can isolate one observation.
    """
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    dead_pid = _spawn_dead_pid()
    record_instance_start(name="target", host="h", pid=dead_pid)
    record_instance_start(name="bystander", host="h", pid=dead_pid)
    cleared = clear_stale_instance_lease("target")
    by_name = {r["name"] for r in list_active_instances()}
    return cleared, by_name


def test_clear_stale_instance_lease_reports_one_when_scoped_to_target(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_name_scoped_scenario
    # Act
    cleared, _by_name = scenario()
    # Assert
    assert cleared == 1


def test_clear_stale_instance_lease_removes_only_named_target(db_path: Path) -> None:
    # Arrange
    scenario = _drive_name_scoped_scenario
    # Act
    _cleared, by_name = scenario()
    # Assert
    assert "target" not in by_name


def test_clear_stale_instance_lease_preserves_bystander_agent_row(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_name_scoped_scenario
    # Act
    _cleared, by_name = scenario()
    # Assert
    assert "bystander" in by_name


# ---------------------------------------------------------------------------
# Null-pid rows are left alone (the helper's narrow contract)
# ---------------------------------------------------------------------------


def _drive_null_pid_scenario(name: str) -> tuple[int, list[dict]]:
    """Arrange + Act helper: write a row with the default (NULL) PID,
    call the cleaner, return the count + post-call active rows.
    """
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    record_instance_start(name=name, host="h")  # pid defaults to None
    cleared = clear_stale_instance_lease(name)
    active = [r for r in list_active_instances() if r["name"] == name]
    return cleared, active


def test_clear_stale_instance_lease_reports_zero_clears_on_null_pid(
    db_path: Path,
) -> None:
    # Arrange
    name = "null-count"
    # Act
    cleared, _active = _drive_null_pid_scenario(name)
    # Assert
    assert cleared == 0


def test_clear_stale_instance_lease_keeps_null_pid_row_active(db_path: Path) -> None:
    # Arrange
    name = "null-active"
    # Act
    _cleared, active = _drive_null_pid_scenario(name)
    # Assert
    assert len(active) == 1


def test_clear_stale_instance_lease_leaves_ended_at_unset_for_null_pid_row(
    db_path: Path,
) -> None:
    # Arrange
    name = "null-ended-at"
    # Act
    _cleared, active = _drive_null_pid_scenario(name)
    # Assert
    assert active[0]["ended_at"] is None


# ---------------------------------------------------------------------------
# Wire-in: agent_start clears the stale row when runtime says dead
# ---------------------------------------------------------------------------


class _DeadRuntime:
    """Real runtime contract: ``is_running`` is False, ``start`` succeeds.

    Mirrors the production runtime's surface — no MagicMock, no
    SimpleNamespace. The start() call records that it was reached so
    the test can assert the start path did not no-op.
    """

    def __init__(self) -> None:
        self.start_calls: list[dict] = []

    def is_running(self, config) -> bool:  # noqa: ANN001
        return False

    def start(self, config, **kw) -> bool:  # noqa: ANN001
        self.start_calls.append(dict(kw))
        return True

    def stop(self, config) -> bool:  # noqa: ANN001
        return True

    def _state_dir(self, config):  # noqa: ANN001
        d = Path(os.environ["HOME"]) / "state" / config.name
        d.mkdir(parents=True, exist_ok=True)
        return d


class _Handover:
    def ensure_instance_uuid(self, _c):
        pass

    def hydrate_from_hub(self, _c):
        pass

    def start_failback_poller(self, _c):
        pass


def _write_zombie_spec(tmp_path: Path) -> Path:
    """Materialise a v3 ``spec.yaml`` for an agent named ``zombie``.

    Production ``load_config`` derives the agent name from the parent
    directory (dir-as-SSoT), so the spec must live at
    ``<name>/spec.yaml``.
    """
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    agent_dir = tmp_path / "zombie"
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    # Red-start ruling 2026-07-21: every field explicit (body wins).
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path / 'work'}\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  health:\n    enabled: true\n    interval: 60\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  hooks:\n"
            "    pre_start: []\n"
            "    post_start: []\n"
            "    pre_stop: []\n"
            "    post_stop: []\n"
        )
    )
    return spec


def _drive_dead_runtime_start_scenario(
    tmp_path: Path,
) -> tuple[_DeadRuntime, list[dict], int]:
    """Arrange + Act helper: pre-seed a stale row with a dead PID,
    drive ``agent_start`` against a runtime that reports ``is_running
    == False``, then return ``(runtime, post_call_rows, dead_pid)``.

    Per-fact tests assert one observation each so a CI failure pinpoints
    the broken contract (start was not reached, or the zombie row
    survived) without coupling them.
    """
    from scitex_agent_container._lifecycle import lifecycle as lc
    from scitex_agent_container._state.registry import Registry
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    spec = _write_zombie_spec(tmp_path)
    dead_pid = _spawn_dead_pid()
    record_instance_start(name="zombie", host="h", pid=dead_pid)

    runtime = _DeadRuntime()
    reg = Registry(registry_dir=tmp_path / "reg")
    lc.agent_start(
        str(spec),
        registry=reg,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=lambda _s: None,
        thread_factory=FakeThread,
    )
    rows = [r for r in list_active_instances() if r["name"] == "zombie"]
    return runtime, rows, dead_pid


def test_agent_start_clears_dead_pid_from_active_zombie_rows(
    pg_schema: str,
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    scenario = _drive_dead_runtime_start_scenario
    # Act
    _runtime, rows, dead_pid = scenario(tmp_path)
    # Assert — no surviving active row carries the dead pid; the start
    # path either ended the zombie row or superseded it with a fresh
    # local-instance row that has a different (NULL or live) pid.
    survivors_with_dead_pid = [r for r in rows if r.get("pid") == dead_pid]
    assert survivors_with_dead_pid == []


def test_agent_start_reaches_runtime_start_after_clearing_zombie_lease(
    pg_schema: str,
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    scenario = _drive_dead_runtime_start_scenario
    # Act
    runtime, _rows, _dead_pid = scenario(tmp_path)
    # Assert — runtime.start was actually invoked (the start path did
    # not no-op on the zombie lease).
    assert runtime.start_calls


# ---------------------------------------------------------------------------
# Wire-in negative: live lease is NOT cleared
# ---------------------------------------------------------------------------


def _drive_live_lease_preserve_scenario() -> tuple[int, int, list[dict]]:
    """Arrange + Act helper: pre-seed a row pointing at the live test
    PID, then call the helper directly. Return the recorded
    ``instance_id``, the cleared count, and the post-call active rows
    for ``livepin`` so per-fact tests can assert one observation each.
    """
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    instance_id = record_instance_start(name="livepin", host="h", pid=os.getpid())
    cleared = clear_stale_instance_lease("livepin")
    rows = [r for r in list_active_instances() if r["name"] == "livepin"]
    return instance_id, cleared, rows


def test_clear_stale_instance_lease_reports_zero_clears_when_runtime_is_alive(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_live_lease_preserve_scenario
    # Act
    _instance_id, cleared, _rows = scenario()
    # Assert
    assert cleared == 0


def test_clear_stale_instance_lease_leaves_exactly_one_live_row_for_livepin(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_live_lease_preserve_scenario
    # Act
    _instance_id, _cleared, rows = scenario()
    # Assert
    assert len(rows) == 1


def test_clear_stale_instance_lease_preserves_original_live_row_id(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_live_lease_preserve_scenario
    # Act
    instance_id, _cleared, rows = scenario()
    # Assert
    assert rows[0]["id"] == instance_id


def test_clear_stale_instance_lease_leaves_ended_at_unset_on_live_row(
    db_path: Path,
) -> None:
    # Arrange
    scenario = _drive_live_lease_preserve_scenario
    # Act
    _instance_id, _cleared, rows = scenario()
    # Assert
    assert rows[0]["ended_at"] is None
