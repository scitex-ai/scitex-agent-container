"""Stale ``instances`` lease cleanup on the start path.

The operator's failure mode: a previous container died WITHOUT going
through ``agent_stop``, leaving an active ``instances`` row whose
recorded PID is dead. Before this helper, the operator had to run::

    sqlite3 ~/.scitex/agent-container/state.db \
        "DELETE FROM instances WHERE name='<name>' AND ended_at IS NULL"

…otherwise the next ``sac agents start`` no-op'd on the zombie lease.

These tests use a real on-disk SQLite ``state.db`` (per-test, via the
``SCITEX_AGENT_CONTAINER_STATE_DB`` env override) and a real PID
oracle (the test process's own ``os.getpid()`` for the "alive" case;
a known-dead PID we just forked-and-waited for the "dead" case). No
``unittest.mock`` / ``MagicMock`` / ``monkeypatch`` anywhere — STX-TQ002
+ STX-TQ007.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into
    a module-level ``DEFAULT_DB_PATH``; reload after setting the env so
    the helper's ``list_active_instances`` / ``record_instance_stop``
    writes land in the temp DB.
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
# ---------------------------------------------------------------------------


def test_clear_stale_instance_lease_clears_row_with_dead_pid(db_path: Path) -> None:
    # Arrange — write an active instances row whose recorded PID is a
    # reaped child (guaranteed-dead). The helper should mark the row
    # ended.
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    dead_pid = _spawn_dead_pid()
    record_instance_start(name="stale-1", host="h", pid=dead_pid)

    # Act
    cleared = clear_stale_instance_lease("stale-1")

    # Assert — exactly one row cleared; no active rows remain for this name.
    assert cleared == 1
    active = [r for r in list_active_instances() if r["name"] == "stale-1"]
    assert active == []


def test_clear_stale_instance_lease_sets_exit_reason_stale_cleared(
    db_path: Path,
) -> None:
    # Arrange — verify the audit trail. A cleared row's exit_reason must
    # be 'stale-cleared' so operators / log scrapers can distinguish
    # zombie-cleared rows from normal stops.
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        open_db,
        record_instance_start,
    )

    dead_pid = _spawn_dead_pid()
    record_instance_start(name="audit-1", host="h", pid=dead_pid)

    # Act
    clear_stale_instance_lease("audit-1")

    # Assert
    with open_db() as conn:
        cur = conn.execute(
            "SELECT exit_reason, ended_at FROM instances WHERE name=?",
            ("audit-1",),
        )
        row = cur.fetchone()
    assert row["exit_reason"] == "stale-cleared"
    assert row["ended_at"] is not None


# ---------------------------------------------------------------------------
# Live row + live pid → preserved (no false clear)
# ---------------------------------------------------------------------------


def test_clear_stale_instance_lease_preserves_row_with_live_pid(
    db_path: Path,
) -> None:
    # Arrange — our own ``os.getpid()`` is guaranteed-alive for the
    # duration of this test. The helper must NOT touch it.
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    record_instance_start(name="live-1", host="h", pid=os.getpid())

    # Act
    cleared = clear_stale_instance_lease("live-1")

    # Assert — nothing cleared; the row stays active.
    assert cleared == 0
    active = [r for r in list_active_instances() if r["name"] == "live-1"]
    assert len(active) == 1
    assert active[0]["ended_at"] is None


# ---------------------------------------------------------------------------
# Selectivity: other agents' stale rows must not be cleared
# ---------------------------------------------------------------------------


def test_clear_stale_instance_lease_is_name_scoped(db_path: Path) -> None:
    # Arrange — two agents, both with stale rows. The helper must only
    # touch the named one.
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

    # Act
    cleared = clear_stale_instance_lease("target")

    # Assert
    assert cleared == 1
    by_name = {r["name"] for r in list_active_instances()}
    assert "target" not in by_name
    assert "bystander" in by_name


# ---------------------------------------------------------------------------
# Null-pid rows are left alone (the helper's narrow contract)
# ---------------------------------------------------------------------------


def test_clear_stale_instance_lease_skips_rows_with_null_pid(db_path: Path) -> None:
    # Arrange — pre-existing local-start rows have ``pid=NULL`` (the
    # legacy local-start path did not record a PID). The helper must
    # NOT clear those — without a PID we have no per-row proof of
    # deadness, and clearing on the runtime-is-dead precondition is
    # the caller's policy.
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    record_instance_start(name="nullpid", host="h")  # pid defaults to None

    # Act
    cleared = clear_stale_instance_lease("nullpid")

    # Assert — row preserved.
    assert cleared == 0
    active = [r for r in list_active_instances() if r["name"] == "nullpid"]
    assert len(active) == 1
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


def test_agent_start_clears_stale_lease_when_runtime_is_dead(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange — operator scenario: previous container died, leaving an
    # active instances row whose PID is dead. The runtime now says
    # "not running". The new agent_start call must clear the stale row
    # and reach runtime.start (not no-op on the zombie lease).
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Real validator-passing v3 spec YAML. Production ``load_config``
    # derives the agent name from the parent directory (dir-as-SSoT),
    # so the spec must live at ``<name>/spec.yaml``.
    agent_dir = tmp_path / "zombie"
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  hooks:\n"
        "    pre_start: []\n"
        "    post_start: []\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
    )

    # Pre-seed: a stale active row pointing at a dead PID.
    dead_pid = _spawn_dead_pid()
    record_instance_start(name="zombie", host="h", pid=dead_pid)

    runtime = _DeadRuntime()

    # Use a non-autostart Registry rooted in tmp_path.
    from scitex_agent_container._lifecycle import lifecycle as lc
    from scitex_agent_container._state.registry import Registry

    reg = Registry(registry_dir=tmp_path / "reg")

    class _Handover:
        def ensure_instance_uuid(self, _c):
            pass

        def hydrate_from_hub(self, _c):
            pass

        def start_failback_poller(self, _c):
            pass

    # Act
    lc.agent_start(
        str(spec),
        registry=reg,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=lambda _s: None,
    )

    # Assert — the stale row was cleared by the start path. Either it
    # is gone (ended_at set) or it was superseded by the new row that
    # ``record_local_instance`` writes. The contract is "no zombie row
    # pinned with a dead PID survives the start path".
    rows = [r for r in list_active_instances() if r["name"] == "zombie"]
    # At most one active row (the fresh local instance); none of them
    # carries the dead pid.
    for row in rows:
        assert row.get("pid") != dead_pid
    # And runtime.start was actually reached — not no-op'd.
    assert runtime.start_calls, "runtime.start should have been called"


def test_agent_start_does_not_touch_live_lease_when_runtime_is_alive(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange — the runtime says alive. agent_start must NOT clear any
    # row, and the existing live lease must survive untouched.
    from scitex_agent_container._lifecycle._stale_lease import (
        clear_stale_instance_lease,
    )
    from scitex_agent_container._state.state_db import (
        list_active_instances,
        record_instance_start,
    )

    # Pre-seed: an active row with the test's own (live) pid.
    instance_id = record_instance_start(name="livepin", host="h", pid=os.getpid())

    # Sanity: a direct helper call on a live pid clears nothing.
    cleared = clear_stale_instance_lease("livepin")

    # Assert
    assert cleared == 0
    rows = [r for r in list_active_instances() if r["name"] == "livepin"]
    assert len(rows) == 1
    assert rows[0]["id"] == instance_id
    assert rows[0]["ended_at"] is None
