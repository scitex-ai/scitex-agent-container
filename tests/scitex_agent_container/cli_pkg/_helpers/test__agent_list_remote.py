"""Tests for the REMOTE-instance read side of ``sac agents list``.

The master ssh-dispatches an agent (spec ``host: spartan``) and already records
an ``instances`` row (``host=<peer>``, ``remote=1``, ``pid=NULL``) on dispatch,
tombstoning it on stop. This suite pins the newly-added READ side:
``get_agent_list_data`` / ``remote_instance_rows`` surface that row as
**running on host <peer>** — with a LIVE ssh probe deciding the status — and the
gc reaper's ``remote=0`` guard never tombstones it locally.

No-mocks, per the repo's STX-TQ rules:

* real on-disk ``state.db`` via the ``isolated_state_db`` fixture (env override +
  module reload — the exact pattern the ``_stale_lease`` / lifecycle suites use),
* all ``instances`` state through the real ``record_instance_start`` /
  ``record_instance_stop`` / ``gc_dead_instances``,
* a real empty ``Registry(tmp_path)``,
* the cross-host probe driven by an injected ``run_ssh`` / ``status_probe``
  (real rc-returning / status-returning callables — never a shell-out, never a
  ``MagicMock``),
* real ``spec.yaml`` files exercised by the real ``load_config`` for labels.

One logical assertion per test (STX-TQ007); AAA markers throughout.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    get_agent_list_data,
    remote_instance_rows,
)

# ---------------------------------------------------------------------------
# Fixtures + seams (copied from the sibling suites — real callables, no mocks).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_db(tmp_path: Path) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (explicit save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into a
    module-level ``DEFAULT_DB_PATH``; reload it after setting the env so the
    ``instances`` writes land in the temp DB, not the developer's real
    ``~/.scitex`` tree. Mirrors ``_lifecycle/test__stale_lease.py``.
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


@contextmanager
def _swap_discover(
    impl: Callable[[], list[tuple[str, Path]]],
) -> Iterator[None]:
    """Swap ``_al._discover_defined_agents`` for a real callable."""
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]


@contextmanager
def _swap_probe(impl: Callable[[Any], bool | None]) -> Iterator[None]:
    """Swap the LOCAL liveness probe (both module + parent-package re-export)."""
    import scitex_agent_container.cli_pkg._helpers as _pkg

    saved_al = _al._probe_local
    saved_pkg = getattr(_pkg, "_probe_local", None)
    _al._probe_local = impl  # type: ignore[assignment]
    _pkg._probe_local = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._probe_local = saved_al  # type: ignore[assignment]
        if saved_pkg is None:
            delattr(_pkg, "_probe_local")
        else:
            _pkg._probe_local = saved_pkg  # type: ignore[assignment]


def _no_discover() -> list[tuple[str, Path]]:
    """Return [] — no on-disk agents interfere with the instances-row merge."""
    return []


def _write_valid_spec(dir_: Path, *, machine: str | None = None) -> Path:
    """Write a minimal real v3 spec.yaml (optionally with a machine label)."""
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    lines = ["apiVersion: scitex-agent-container/v3", "kind: Agent"]
    if machine is not None:
        lines += ["metadata:", "  labels:", f'    machine: "{machine}"']
    else:
        lines.append("metadata: {}")
    lines += [
        "spec:",
        "  runtime: apptainer",
        "  host: ${HOSTNAME}",
        "  workdir: /home/agent/work",
        "  apptainer:",
        "    image: /x.sif",
        "    binds: []",
        "  claude:",
        "    model: sonnet",
        "  health:",
        "    enabled: true",
        "    interval: 60",
        "  restart:",
        "    policy: on-failure",
        "    max_retries: 3",
    ]
    spec.write_text("\n".join(lines) + "\n")
    return spec


# ---------------------------------------------------------------------------
# Drive helpers — real record_instance_start + the two entry points.
# ---------------------------------------------------------------------------


def _record_remote(
    name: str = "spartan-dev", host: str = "spartan", a2a: int = 8123
) -> str:
    """Record a cross-host (remote=1) active instances row; return its id."""
    from scitex_agent_container._state.state_db import record_instance_start

    return record_instance_start(name=name, host=host, a2a_port=a2a, remote=True)


def _list_rows(
    tmp_path: Path,
    *,
    run_ssh: Callable[[list[str]], int] = lambda argv: 0,
    discover: Callable[[], list[tuple[str, Path]]] = _no_discover,
    registry: Registry | None = None,
    **kw: Any,
) -> list[dict]:
    """Drive ``get_agent_list_data`` with an injected (never-shell-out) ssh
    probe. ``run_ssh`` default returns rc 0 (ALIVE -> running)."""
    reg = registry or Registry(registry_dir=tmp_path / "reg")
    with _swap_discover(discover):
        return get_agent_list_data(reg, remote_run_ssh=run_ssh, **kw)


def _remote_rows_direct(
    *,
    run_ssh: Callable[[list[str]], int],
    discover: Callable[[], list[tuple[str, Path]]] = _no_discover,
) -> list[dict]:
    """Drive ``remote_instance_rows`` straight (real instances oracle)."""
    with _swap_discover(discover):
        return remote_instance_rows(
            registered=set(),
            display_host="master-host",
            port_claims={},
            run_ssh=run_ssh,
        )


def _spartan_row(rows: list[dict]) -> dict:
    return next(r for r in rows if r["name"] == "spartan-dev")


# ---------------------------------------------------------------------------
# 1. A remote-dispatched agent surfaces as running-on-peer (probe ALIVE).
# ---------------------------------------------------------------------------


def test_remote_dispatched_agent_surfaces_exactly_once(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — surfaces AND is not duplicated as a defined/local row.
    assert [r["name"] for r in rows].count("spartan-dev") == 1


def test_remote_row_status_is_running_when_probe_alive(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act — rc 0 == tmux session up on the peer == ALIVE.
    rows = _list_rows(tmp_path, run_ssh=lambda argv: 0)
    # Assert
    assert _spartan_row(rows)["status"] == "running"


def test_remote_row_reports_the_peer_host(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["host"] == "spartan"


def test_remote_row_host_display_is_the_peer(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — a concrete peer host passes through the display mapping unchanged.
    assert _spartan_row(rows)["host_display"] == "spartan"


def test_remote_row_carries_the_bound_a2a_port(isolated_state_db, tmp_path):
    # Arrange
    _record_remote(a2a=8123)
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["a2a_port"] == 8123


def test_remote_row_is_flagged_remote(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["remote"] is True


def test_remote_row_is_not_emitted_as_defined_local(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — the only row for the name is the remote one (host != "local").
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["host"] == "spartan"


# ---------------------------------------------------------------------------
# 2. LIVE probe verdict -> status (via remote_process_signal + injected run_ssh).
# ---------------------------------------------------------------------------


def test_remote_probe_dead_maps_to_stopped(isolated_state_db):
    # Arrange — rc 1 == ssh connected, peer tmux has NO session == DEAD.
    _record_remote()
    # Act
    rows = _remote_rows_direct(run_ssh=lambda argv: 1)
    # Assert
    assert _spartan_row(rows)["status"] == "stopped"


def test_remote_probe_unknown_maps_to_running(isolated_state_db):
    # Arrange — rc 255 == wedged/auth/bare-PATH ssh == UNKNOWN (never hide).
    _record_remote()
    # Act
    rows = _remote_rows_direct(run_ssh=lambda argv: 255)
    # Assert
    assert _spartan_row(rows)["status"] == "running"


# ---------------------------------------------------------------------------
# 3. Precedence: a LOCAL registry row wins over the remote instances row.
# ---------------------------------------------------------------------------


def test_local_registry_row_wins_over_remote_instance(isolated_state_db, tmp_path):
    # Arrange — same name registered locally AND active as a remote instance.
    spec = _write_valid_spec(tmp_path / "spartan-dev")
    registry = Registry(registry_dir=tmp_path / "reg")
    registry.add("spartan-dev", str(spec), "tui-spartan-dev")
    _record_remote()
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        rows = get_agent_list_data(registry, remote_run_ssh=lambda argv: 0)
    # Assert — one row, and it is the local one.
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["host"] == "local"


# ---------------------------------------------------------------------------
# 4. A tombstoned (stopped) remote row disappears from the list.
# ---------------------------------------------------------------------------


def test_tombstoned_remote_row_disappears(isolated_state_db, tmp_path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_stop

    instance_id = _record_remote()
    record_instance_stop(instance_id)
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert [r for r in rows if r["name"] == "spartan-dev"] == []


# ---------------------------------------------------------------------------
# 5. The gc reaper never tombstones a remote row (the remote=0 guard).
# ---------------------------------------------------------------------------


def test_reaper_leaves_remote_row_active(isolated_state_db):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
    )

    _record_remote()
    # Act — a full sweep must not touch the cross-host row.
    gc_dead_instances(dry_run=False)
    # Assert
    active = [r for r in list_active_instances(host=None) if r["name"] == "spartan-dev"]
    assert len(active) == 1


def test_reaper_remote_guard_keeps_row_with_forced_stale_heartbeat(isolated_state_db):
    # Arrange — force a long-stale last_heartbeat_at onto the remote row; only
    # the ``AND remote=0`` guard keeps the heartbeat sweep from reaping it.
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
        open_db,
    )

    instance_id = _record_remote()
    with open_db() as conn:
        conn.execute(
            "UPDATE instances SET last_heartbeat_at=? WHERE id=?",
            ("2000-01-01T00:00:00Z", instance_id),
        )
    # Act — a 1s staleness cutoff would trip the sweep but for the guard.
    gc_dead_instances(dry_run=False, heartbeat_stale_seconds=1)
    # Assert
    active = [r for r in list_active_instances(host=None) if r["name"] == "spartan-dev"]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# 6. The default RUNNING-ONLY view retains a running remote row.
# ---------------------------------------------------------------------------


def test_running_only_view_retains_running_remote_row(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path, running_only=True, run_ssh=lambda argv: 0)
    # Assert
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["status"] == "running"


# ---------------------------------------------------------------------------
# 7. Label filters (machine) include / exclude the remote row correctly.
# ---------------------------------------------------------------------------


def test_machine_label_includes_matching_remote_row(isolated_state_db, tmp_path):
    # Arrange — spec on disk carries machine="spartan"; the instances row is remote.
    spec = _write_valid_spec(tmp_path / "spartan-dev", machine="spartan")
    _record_remote()

    def _disc() -> list[tuple[str, Path]]:
        return [("spartan-dev", spec)]

    # Act
    rows = _list_rows(tmp_path, machine="spartan", discover=_disc)
    # Assert
    assert any(r["name"] == "spartan-dev" and r.get("remote") for r in rows)


def test_machine_label_excludes_non_matching_remote_row(isolated_state_db, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "spartan-dev", machine="spartan")
    _record_remote()

    def _disc() -> list[tuple[str, Path]]:
        return [("spartan-dev", spec)]

    # Act
    rows = _list_rows(tmp_path, machine="nuc", discover=_disc)
    # Assert
    assert [r for r in rows if r["name"] == "spartan-dev"] == []
